// Copyright (c) 2026 Mike Kovetsky
// SPDX-License-Identifier: Apache-2.0
//
// Replays golden-model traces (test/lockstep/traces.py) into the RTL core and compares on every clock the
// pad registers (out, oe), the overflow flag, and the answers to the trace's host calls (room, a popped byte
// or none), not the registered host status. It stops a trace at its first mismatch and prints the step, the
// signal, what the model gave and what the RTL gave.
//
//   lockstep FILE.trace [FILE.trace ...]
//
// One model step is one rising edge. The core is reset, the trace's image goes in through the loader one
// byte per clock, and step 0 is the first edge after `running` rises. Before step k's edge the pins are
// the ones the model saw in step k, and the host calls the model made right after step k are applied, so
// their answers are read before that same edge (the core keeps the model's order: machine first, host
// second, inside one clock).

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "Vprotoemu_core.h"
#include "verilated.h"

namespace {

struct Event {
  uint32_t step;
  uint8_t kind, ch, value;
};

struct Trace {
  std::string name;
  std::vector<uint8_t> image;
  std::vector<uint8_t> steps;  // pins, out, oe
  std::vector<Event> events;
  uint32_t overflow_step = 0xFFFFFFFFu;
};

enum { K_CAN_PUSH = 0, K_PUSH = 1, K_POP = 2, K_POP_NONE = 3 };

class Reader {
 public:
  explicit Reader(const char *path) {
    FILE *f = std::fopen(path, "rb");
    if (!f) return;
    uint8_t chunk[65536];
    size_t n;
    while ((n = std::fread(chunk, 1, sizeof chunk, f)) > 0) data_.insert(data_.end(), chunk, chunk + n);
    std::fclose(f);
  }
  bool ok() const { return !bad_; }
  bool empty() const { return data_.empty(); }
  uint32_t u32() {
    if (pos_ + 4 > data_.size()) return fail();
    uint32_t v = data_[pos_] | data_[pos_ + 1] << 8 | data_[pos_ + 2] << 16 | (uint32_t)data_[pos_ + 3] << 24;
    pos_ += 4;
    return v;
  }
  uint16_t u16() {
    if (pos_ + 2 > data_.size()) return fail();
    uint16_t v = data_[pos_] | data_[pos_ + 1] << 8;
    pos_ += 2;
    return v;
  }
  uint8_t u8() {
    if (pos_ + 1 > data_.size()) return fail();
    return data_[pos_++];
  }
  std::vector<uint8_t> bytes(size_t n) {
    if (pos_ + n > data_.size()) {
      fail();
      return {};
    }
    std::vector<uint8_t> v(data_.begin() + pos_, data_.begin() + pos_ + n);
    pos_ += n;
    return v;
  }

 private:
  uint32_t fail() {
    bad_ = true;
    pos_ = data_.size();
    return 0;
  }
  std::vector<uint8_t> data_;
  size_t pos_ = 0;
  bool bad_ = false;
};

bool read_traces(const char *path, std::vector<Trace> &out) {
  Reader r(path);
  if (r.empty()) return false;
  std::vector<uint8_t> magic = r.bytes(4);
  if (magic.size() != 4 || std::memcmp(magic.data(), "PETR", 4) != 0) return false;
  if (r.u32() != 1) return false;
  uint32_t count = r.u32();
  for (uint32_t i = 0; i < count && r.ok(); i++) {
    Trace t;
    std::vector<uint8_t> name = r.bytes(r.u16());
    t.name.assign(name.begin(), name.end());
    t.image = r.bytes(r.u32());
    t.steps = r.bytes(3 * (size_t)r.u32());
    uint32_t n_events = r.u32();
    for (uint32_t e = 0; e < n_events && r.ok(); e++) {
      Event ev;
      ev.step = r.u32();
      ev.kind = r.u8();
      ev.ch = r.u8();
      ev.value = r.u8();
      r.u8();
      t.events.push_back(ev);
    }
    t.overflow_step = r.u32();
    out.push_back(std::move(t));
  }
  return r.ok();
}

class Dut {
 public:
  Dut() : ctx_(new VerilatedContext), top_(new Vprotoemu_core{ctx_.get()}) {}
  ~Dut() { top_->final(); }
  Vprotoemu_core *operator->() { return top_.get(); }
  void clear_inputs() {
    top_->pins = 0;
    top_->pins_half = 0;
    top_->ld_valid = 0;
    top_->ld_byte = 0;
    top_->h_push0 = 0;
    top_->h_data0 = 0;
    top_->h_push3 = 0;
    top_->h_data3 = 0;
    top_->h_pop1 = 0;
    top_->h_pop2 = 0;
  }
  // Inputs are set with the clock low; `settle` makes the combinational outputs valid before the edge.
  void settle() {
    top_->clk = 0;
    top_->eval();
  }
  void edge() {
    top_->clk = 1;
    top_->eval();
    top_->clk = 0;
    top_->eval();
  }
  void reset() {
    clear_inputs();
    top_->rst_n = 1;
    settle();
    top_->rst_n = 0;
    settle();
    for (int i = 0; i < 3; i++) edge();
    top_->rst_n = 1;
    settle();
  }

 private:
  std::unique_ptr<VerilatedContext> ctx_;
  std::unique_ptr<Vprotoemu_core> top_;
};

struct Mismatch {
  bool found = false;
  char text[256] = {0};
};

#define MISMATCH(...)                                              \
  do {                                                             \
    std::snprintf(m.text, sizeof m.text, __VA_ARGS__);             \
    m.found = true;                                                \
    return m;                                                      \
  } while (0)

Mismatch replay(Dut &dut, const Trace &t, uint64_t &compared) {
  Mismatch m;
  compared = 0;
  dut.reset();

  // Load the image through the loader port.
  size_t taken = 0;
  long load_clocks = 0;
  while (!dut->running) {
    dut->ld_valid = taken < t.image.size();
    dut->ld_byte = taken < t.image.size() ? t.image[taken] : 0;
    dut.settle();
    bool took = dut->ld_valid && dut->ld_ready;
    dut.edge();
    if (took) taken++;
    if (++load_clocks > 200000) MISMATCH("loader: never started running (%zu of %zu bytes taken)", taken, t.image.size());
  }
  dut->ld_valid = 0;
  if (taken != t.image.size()) MISMATCH("loader: started running after %zu of %zu image bytes", taken, t.image.size());
  if (dut->pin_out != 0 || dut->pin_oe != 0)
    MISMATCH("before step 0: out %02x oe %02x, expected 00 00", dut->pin_out, dut->pin_oe);

  size_t n_steps = t.steps.size() / 3;
  size_t e = 0;
  for (size_t k = 0; k < n_steps; k++) {
    dut->pins = t.steps[3 * k];
    dut->pins_half = t.steps[3 * k];
    dut->h_push0 = dut->h_push3 = dut->h_pop1 = dut->h_pop2 = 0;
    size_t first = e;
    while (e < t.events.size() && t.events[e].step == k) {
      const Event &ev = t.events[e++];
      if (ev.kind == K_PUSH) {
        if (ev.ch == 0) {
          dut->h_push0 = 1;
          dut->h_data0 = ev.value;
        } else if (ev.ch == 3) {
          dut->h_push3 = 1;
          dut->h_data3 = ev.value;
        } else {
          MISMATCH("step %zu: the model took a host push on channel %d", k, ev.ch);
        }
      } else if (ev.kind == K_POP || ev.kind == K_POP_NONE) {
        if (ev.ch == 1) dut->h_pop1 = 1;
        if (ev.ch == 2) dut->h_pop2 = 1;
      }
    }
    if (e < t.events.size() && t.events[e].step < k) MISMATCH("trace events out of order at step %zu", k);
    dut.settle();

    // The host's answers, as the model gave them right after step k.
    for (size_t i = first; i < e; i++) {
      const Event &ev = t.events[i];
      if (ev.kind == K_CAN_PUSH) {
        int got = ev.ch == 0 ? dut->h_room0 : ev.ch == 3 ? dut->h_room3 : 0;
        if (got != ev.value) MISMATCH("step %zu: host_can_push(%d) expected %d got %d", k, ev.ch, ev.value, got);
      } else if (ev.kind == K_POP || ev.kind == K_POP_NONE) {
        int has = ev.ch == 1 ? dut->h_has1 : dut->h_has2;
        int q = ev.ch == 1 ? dut->h_q1 : dut->h_q2;
        if (ev.kind == K_POP_NONE && has)
          MISMATCH("step %zu: host_pop(%d) expected None got 0x%02x", k, ev.ch, q);
        if (ev.kind == K_POP && !has) MISMATCH("step %zu: host_pop(%d) expected 0x%02x got None", k, ev.ch, ev.value);
        if (ev.kind == K_POP && q != ev.value)
          MISMATCH("step %zu: host_pop(%d) expected 0x%02x got 0x%02x", k, ev.ch, ev.value, q);
      }
    }

    dut.edge();
    compared++;
    uint8_t want_out = t.steps[3 * k + 1], want_oe = t.steps[3 * k + 2];
    if (dut->pin_out != want_out) MISMATCH("step %zu: out expected 0x%02x got 0x%02x", k, want_out, dut->pin_out);
    if (dut->pin_oe != want_oe) MISMATCH("step %zu: oe expected 0x%02x got 0x%02x", k, want_oe, dut->pin_oe);
    int want_overflow = t.overflow_step != 0xFFFFFFFFu && k >= t.overflow_step;
    if (dut->overflow != want_overflow)
      MISMATCH("step %zu: overflow expected %d got %d", k, want_overflow, dut->overflow);
  }
  if (e != t.events.size()) MISMATCH("%zu host events after the last step", t.events.size() - e);
  return m;
}

}  // namespace

int main(int argc, char **argv) {
  if (argc < 2) {
    std::fprintf(stderr, "usage: %s FILE.trace [FILE.trace ...]\n", argv[0]);
    return 2;
  }
  Dut dut;
  int failed_traces = 0, total_traces = 0;
  uint64_t total_steps = 0;
  for (int a = 1; a < argc; a++) {
    std::vector<Trace> traces;
    if (!read_traces(argv[a], traces)) {
      std::printf("FAIL %s: cannot read the trace file\n", argv[a]);
      failed_traces++;
      continue;
    }
    uint64_t file_steps = 0;
    int file_failed = 0;
    for (const Trace &t : traces) {
      uint64_t compared = 0;
      Mismatch m = replay(dut, t, compared);
      file_steps += compared;
      total_traces++;
      if (m.found) {
        file_failed++;
        std::printf("FAIL %s: %s\n", t.name.c_str(), m.text);
      }
    }
    failed_traces += file_failed;
    total_steps += file_steps;
    const char *base = std::strrchr(argv[a], '/');
    std::printf("%s %-28s %3zu traces, %9llu steps compared%s\n", file_failed ? "FAIL" : "pass",
                base ? base + 1 : argv[a], traces.size(), (unsigned long long)file_steps,
                file_failed ? "" : ", all match");
  }
  std::printf("lockstep: %d of %d traces match, %llu steps compared\n", total_traces - failed_traces, total_traces,
              (unsigned long long)total_steps);
  return failed_traces ? 1 : 0;
}
