// Copyright (c) 2026 Mike Kovetsky
// SPDX-License-Identifier: Apache-2.0
//
// The bench for learn-and-become: the whole chip (src/project.v, Verilated) on a board.
//
//   host   talks through ui_in and uo_out only: nibble link, loader, RUNNING, OVERFLOW (docs/info.md).
//   pins   P0..P7 are the wired-AND of the chip, at most one table-driven device and one far end that Python
//          drives. An open-drain pin that is let go takes `rise` clocks to read high.
//   device a bit-level SPI or I2C target round a byte-level table. The table is the hidden truth: it lives in
//          the bench's process and nothing on the host side of the link can read it.
//
// Time is whole clocks of 60 MHz. learn/lab/board.py is the Python side.

#include <cstdint>
#include <cstring>
#include <memory>
#include <vector>

#include "Vtt_um_mikekovetsky_proteus.h"
#include "verilated.h"
#include "verilated_save.h"

namespace {

constexpr uint8_t ST_ROOM = 0x10, ST_HAS = 0x20, ST_RUNNING = 0x40, ST_OVERFLOW = 0x80;
constexpr int PORT_DATA = 0, PORT_STATUS = 1, PORT_LOADER = 2;
constexpr int NRESET = 4;

enum { DEV_NONE = 0, DEV_SPI = 1, DEV_I2C = 2 };

class MemSave final : public VerilatedSerialize {
 public:
  std::vector<uint8_t> data;
  void flush() override {
    data.insert(data.end(), m_bufp, m_cp);
    m_cp = m_bufp;
  }
};

class MemRestore final : public VerilatedDeserialize {
 public:
  MemRestore(const uint8_t *p, size_t n) : src_(p), end_(p + n) { m_endp = m_cp; }
  void fill() override {
    size_t left = m_endp - m_cp;
    std::memmove(m_bufp, m_cp, left);
    size_t take = std::min<size_t>(bufferSize() - left, end_ - src_);
    std::memcpy(m_bufp + left, src_, take);
    src_ += take;
    m_cp = m_bufp;
    m_endp = m_bufp + left + take;
  }

 private:
  const uint8_t *src_, *end_;
};

// What an imperfect device does besides its table: the negative controls.
struct Quirks {
  uint32_t flip_ppm = 0;      // one byte in a million that it sends has bit 0 flipped
  uint32_t seed = 1;          // for the above
  uint32_t timeout = 0;       // after this many clocks of a still bus it moves to state idle_next[state]
  uint32_t keeps_state = 0;   // nRESET does not restore state 0
};

struct Table {
  int n = 0;
  std::vector<uint8_t> out;       // the byte a state sends
  std::vector<uint8_t> mask;      // SPI: XOR on that byte's later bits once the first bit received is 1
  std::vector<int32_t> next;      // SPI: n x 256 by byte. I2C: n x 512, data bytes then bytes after START
  std::vector<uint8_t> ack;       // I2C: the same shape, 1 for ACK
  std::vector<int32_t> a, b, c;   // SPI: select, deselect. I2C: read+ACK, read+NACK, STOP
  std::vector<int32_t> idle_next; // with Quirks::timeout
};

class Board {
 public:
  Board(uint8_t pull, uint32_t seed) : ctx_(new VerilatedContext), pull_(pull) {
    ctx_->randReset(2);  // power-up values are random: nothing may lean on them
    ctx_->randSeed(static_cast<int>(seed | 1));
    top_.reset(new Vtt_um_mikekovetsky_proteus{ctx_.get()});
    top_->ena = 1;
    top_->clk = 0;
    top_->rst_n = 0;
    top_->ui_in = 0;
    wire_ = line_ = pull_;
    top_->uio_in = line_;
    top_->eval();
    last_line_ = line_;
  }
  ~Board() { top_->final(); }

  // ---- the device

  void plug(int kind, int delay, const Table &t, const Quirks &q) {
    kind_ = kind;
    delay_ = delay;
    t_ = t;
    q_ = q;
    rnd_ = q.seed ? q.seed : 1;
    d_ = Dev();
    d_mask_ = d_val_ = 0;
    pend_ = false;
    resolve();
  }
  void unplug() { plug(DEV_NONE, 0, Table(), Quirks()); }

  // ---- the host's side: ui_in and uo_out only

  int load(const uint8_t *image, int n) {
    nib_ = port_ = wr_ = rd_ = 0;
    drive_host();
    top_->rst_n = 0;
    top_->eval();
    resolve();
    run(10);
    top_->rst_n = 1;
    run(5);
    for (int i = 0; i < n; i++) {
      select(PORT_LOADER);
      if (!wait_for(ST_ROOM, 2000)) return -1;
      write(image[i]);
    }
    select(PORT_LOADER);
    if (!wait_for(ST_RUNNING, 2000)) return -2;
    select(PORT_DATA);
    run(20);
    return 0;
  }

  // Writes tx to channel 0 and reads nrx bytes of channel 1. Channel 2 is looked at before it returns.
  int exchange(const uint8_t *tx, int ntx, uint8_t *rx, int nrx, uint8_t *rx2, int cap2, int *n2, uint64_t limit) {
    int sent = 0, got = 0;
    *n2 = 0;
    uint64_t end = cycle_ + limit;
    select(PORT_DATA);
    while ((sent < ntx || got < nrx) && cycle_ < end) {
      bool busy = false;
      if (sent < ntx && (status() & ST_ROOM)) {
        write(tx[sent++]);
        busy = true;
      }
      if (got < nrx && (status() & ST_HAS)) {
        rx[got++] = read();
        busy = true;
      }
      if (!busy) run(1);
    }
    select(PORT_STATUS);
    while ((status() & ST_HAS) && *n2 < cap2) rx2[(*n2)++] = read();
    select(PORT_DATA);
    return sent < ntx ? -1 : got;
  }

  // ---- the pins' side

  void far(uint8_t mask, uint8_t val) {
    f_mask_ = mask;
    f_val_ = val;
    resolve();
  }
  void set_rise(uint8_t mask, int clocks) {
    rise_mask_ = mask;
    rise_ = clocks;
  }
  void run(uint64_t n) {
    for (uint64_t i = 0; i < n; i++) tick();
  }

  void get(uint64_t *out) const {
    out[0] = cycle_;
    out[1] = line_;
    out[2] = top_->uio_oe;
    out[3] = top_->uio_out;
    out[4] = top_->uo_out;
    out[5] = contention_;
    out[6] = chip_high_;
    out[7] = d_mask_;
  }
  void clear_faults() {
    contention_ = 0;
    chip_high_ = 0;
  }

  // ---- the log: one record per clock in which a pin's level or driver changed

  struct Record {
    uint64_t cycle;
    uint8_t line, chip_oe, chip_out, dev_oe, pad[4];
  };
  void log(bool on) {
    logging_ = on;
    log_.clear();
    if (on) record();
  }
  const std::vector<Record> &records() const { return log_; }

  // ---- the chip's whole state, for the judge's product exploration

  const std::vector<uint8_t> &save() {
    MemSave s;
    uint8_t e[] = {line_, wire_, f_mask_, f_val_, nib_, port_, wr_, rd_};
    s.write(e, sizeof e);
    s << *top_;
    s.flush();
    saved_ = std::move(s.data);
    return saved_;
  }
  void restore(const uint8_t *p, size_t n) {
    MemRestore r(p, n);
    uint8_t e[8];
    r.read(e, sizeof e);
    r >> *top_;
    line_ = last_line_ = e[0];
    wire_ = e[1];
    f_mask_ = e[2];
    f_val_ = e[3];
    nib_ = e[4];
    port_ = e[5];
    wr_ = e[6];
    rd_ = e[7];
    for (uint64_t &t : high_at_) t = 0;
    drive_host();
    top_->uio_in = line_;
  }

 private:
  struct Dev {
    int state = 0, bits = 0;
    uint8_t shift = 0, cur = 0;
    bool active = false, sample = false;
    int mode = 0;
  };
  enum { IDLE, ADDR, WRITE, READ_NEXT, READ, IGNORE };  // READ_NEXT: the ACK of a read's address

  // ---- one clock

  void tick() {
    top_->clk = 1;
    top_->eval();
    top_->clk = 0;
    top_->eval();
    cycle_++;
    uint8_t oe = top_->uio_oe, out = top_->uio_out & oe;
    if (pend_ && cycle_ >= pend_at_) put();
    if (dirty_ || oe != c_oe_ || out != c_out_ || line_ != wire_) {
      dirty_ = false;
      resolve();
    }
  }

  void resolve() {
    for (int round = 0; round < 4; round++) {
      c_oe_ = top_->uio_oe;
      c_out_ = top_->uio_out & c_oe_;
      chip_high_ |= c_out_;
      uint8_t oe = c_oe_ | d_mask_ | f_mask_;
      uint8_t high = c_out_ | (d_mask_ & d_val_) | (f_mask_ & f_val_);
      uint8_t low = (c_oe_ & ~c_out_) | (d_mask_ & ~d_val_) | (f_mask_ & ~f_val_);
      uint8_t clash = high & low;
      if (clash) contention_++;
      uint8_t wire = (high | (pull_ & ~oe)) & ~low;
      uint8_t line = wire;
      if (rise_mask_) {
        for (int pin = 0; pin < 8; pin++) {
          uint8_t bit = 1u << pin;
          if (!(rise_mask_ & bit) || !(wire & bit) || (oe & bit)) continue;
          if (!(wire_ & bit)) high_at_[pin] = cycle_ + rise_;
          if (cycle_ < high_at_[pin]) line &= ~bit;
        }
      }
      wire_ = wire;
      line_ = line;
      if (line == last_line_) break;
      uint8_t before = last_line_;
      last_line_ = line;
      if (kind_ != DEV_NONE) device(before, line);
    }
    top_->uio_in = line_;
    if (logging_) record();
  }

  void record() {
    if (!log_.empty()) {
      const Record &r = log_.back();
      if (r.line == line_ && r.chip_oe == c_oe_ && r.chip_out == c_out_ && r.dev_oe == d_mask_) return;
    }
    log_.push_back(Record{cycle_, line_, c_oe_, c_out_, d_mask_, {0, 0, 0, 0}});
  }

  // ---- the device's bit level

  void schedule(int pin, int level) {  // level 2 releases the pin
    pend_ = true;
    pend_at_ = cycle_ + delay_;
    pend_pin_ = pin;
    pend_level_ = level;
    if (delay_ == 0) put();
  }
  void put() {
    uint8_t bit = 1u << pend_pin_;
    pend_ = false;
    d_mask_ = pend_level_ == 2 ? (d_mask_ & ~bit) : (d_mask_ | bit);
    d_val_ = pend_level_ == 1 ? (d_val_ | bit) : (d_val_ & ~bit);
    dirty_ = true;
  }

  uint8_t sent(int state) {
    uint8_t v = t_.out[state];
    if (q_.flip_ppm) {
      rnd_ ^= rnd_ << 13;
      rnd_ ^= rnd_ >> 17;
      rnd_ ^= rnd_ << 5;
      if (rnd_ % 1000000u < q_.flip_ppm) v ^= 1;
    }
    return v;
  }

  void device(uint8_t before, uint8_t line) {
    if (q_.timeout && cycle_ - moved_at_ > q_.timeout) d_.state = t_.idle_next[d_.state];
    moved_at_ = cycle_;
    uint8_t rst = 1u << NRESET;
    if (!(line & rst)) {
      if (before & rst) {
        int keep = d_.state;
        d_ = Dev();
        if (q_.keeps_state) d_.state = keep;
        pend_ = false;
        d_mask_ = 0;
      }
      return;
    }
    if (kind_ == DEV_SPI) spi(before, line); else i2c(before, line);
  }

  void spi(uint8_t before, uint8_t line) {
    const uint8_t SCK = 1, MOSI = 2, CS = 8;
    const int MISO = 2;
    uint8_t changed = before ^ line;
    if (changed & CS) {
      if (line & CS) {
        if (d_.active) d_.state = t_.b[d_.state];
        d_.active = false;
        schedule(MISO, 2);
      } else {
        d_.active = true;
        d_.state = t_.a[d_.state];
        d_.bits = 0;
        d_.cur = sent(d_.state);
        schedule(MISO, d_.cur >> 7);
      }
      return;
    }
    if (!d_.active || !(changed & SCK)) return;
    if (line & SCK) {
      d_.shift = static_cast<uint8_t>(d_.shift << 1 | ((line & MOSI) ? 1 : 0));
      d_.bits++;
      if (d_.bits == 1 && (d_.shift & 1) && !t_.mask.empty()) d_.cur ^= t_.mask[d_.state] & 0x7F;
      if (d_.bits == 8) {
        d_.state = t_.next[d_.state * 256 + d_.shift];
        d_.bits = 0;
        d_.cur = sent(d_.state);
      }
    } else {
      schedule(MISO, d_.cur >> (7 - d_.bits) & 1);
    }
  }

  void i2c(uint8_t before, uint8_t line) {
    const uint8_t SCL = 1, SDA = 2;
    const int P_SDA = 1;
    uint8_t changed = before ^ line;
    if ((changed & SDA) && (before & SCL) && (line & SCL)) {
      d_.bits = 0;
      if (!(line & SDA)) {  // START
        d_.active = true;
        d_.mode = ADDR;
        d_.sample = false;
      } else {  // STOP
        if (d_.active) d_.state = t_.c[d_.state];
        d_.active = false;
        d_.mode = IDLE;
      }
      pend_ = false;
      if (d_mask_) schedule(P_SDA, 2);
      return;
    }
    if (!(changed & SCL) || d_.mode == IDLE || d_.mode == IGNORE) return;
    if (line & SCL) {  // SCL rises: both sides sample
      d_.sample = true;
      if (d_.bits < 8) {
        d_.shift = static_cast<uint8_t>(d_.shift << 1 | ((line & SDA) ? 1 : 0));
      } else if (d_.mode == READ) {  // the controller's ACK
        bool nack = line & SDA;
        d_.state = nack ? t_.b[d_.state] : t_.a[d_.state];
        if (nack) d_.mode = IGNORE;
      }
      d_.bits++;
      return;
    }
    if (!d_.sample) return;  // the fall that follows START
    d_.sample = false;
    if (d_.bits == 8) {
      if (d_.mode == READ) {
        schedule(P_SDA, 2);
        return;
      }
      int at = d_.state * 512 + (d_.mode == ADDR ? 256 : 0) + d_.shift;
      bool ack = t_.ack[at];
      d_.state = t_.next[at];
      if (d_.mode == ADDR) d_.mode = (d_.shift & 1) ? READ_NEXT : WRITE;
      if (ack) schedule(P_SDA, 0); else d_.mode = IGNORE;
    } else if (d_.bits >= 9) {
      d_.bits = 0;
      if (d_.mode == READ_NEXT) d_.mode = READ;
      if (d_.mode == READ) {
        d_.cur = sent(d_.state);
        schedule(P_SDA, (d_.cur & 0x80) ? 2 : 0);
      } else {
        schedule(P_SDA, 2);
      }
    } else if (d_.mode == READ) {
      schedule(P_SDA, (d_.cur >> (7 - d_.bits) & 1) ? 2 : 0);
    }
  }

  // ---- the nibble link

  uint8_t status() const { return top_->uo_out; }
  void drive_host() { top_->ui_in = static_cast<uint8_t>(nib_ | port_ << 4 | wr_ << 6 | rd_ << 7); }
  void select(int port) {
    if (port == port_) return;
    port_ = port;
    drive_host();
    run(4);
  }
  bool wait_for(uint8_t mask, int limit) {
    for (int i = 0; i < limit; i++) {
      if (status() & mask) return true;
      run(1);
    }
    return false;
  }
  void write(uint8_t byte) {
    for (int half = 0; half < 2; half++) {
      nib_ = half ? byte >> 4 : byte & 15;
      drive_host();
      run(1);
      wr_ ^= 1;
      drive_host();
      run(4);
    }
  }
  uint8_t read() {
    uint8_t low = status() & 15;
    rd_ ^= 1;
    drive_host();
    run(4);
    uint8_t high = status() & 15;
    rd_ ^= 1;
    drive_host();
    run(4);
    return static_cast<uint8_t>(low | high << 4);
  }

  std::unique_ptr<VerilatedContext> ctx_;
  std::unique_ptr<Vtt_um_mikekovetsky_proteus> top_;
  uint64_t cycle_ = 0, contention_ = 0, chip_high_ = 0;
  uint8_t pull_, wire_ = 0, line_ = 0, last_line_ = 0;
  uint8_t c_oe_ = 0, c_out_ = 0, d_mask_ = 0, d_val_ = 0, f_mask_ = 0, f_val_ = 0;
  uint8_t rise_mask_ = 0;
  int rise_ = 0;
  uint64_t high_at_[8] = {};
  uint8_t nib_ = 0, port_ = 0, wr_ = 0, rd_ = 0;
  int kind_ = DEV_NONE, delay_ = 0;
  Table t_;
  Quirks q_;
  Dev d_;
  uint32_t rnd_ = 1;
  uint64_t moved_at_ = 0;
  bool pend_ = false, dirty_ = false;
  uint64_t pend_at_ = 0;
  int pend_pin_ = 0, pend_level_ = 0;
  bool logging_ = false;
  std::vector<Record> log_;
  std::vector<uint8_t> saved_;
};

template <typename T>
void fill(std::vector<T> &v, const T *p, size_t n) {
  if (p) v.assign(p, p + n); else v.clear();
}

}  // namespace

extern "C" {
void *brd_new(uint8_t pull, uint32_t seed) { return new Board(pull, seed); }
void brd_free(void *b) { delete static_cast<Board *>(b); }
int brd_plug(void *b, int kind, int delay, int n, const uint8_t *out, const uint8_t *mask, const int32_t *next,
             const uint8_t *ack, const int32_t *a, const int32_t *bb, const int32_t *c, const int32_t *idle_next,
             uint32_t flip_ppm, uint32_t seed, uint32_t timeout, uint32_t keeps_state) {
  if (kind == DEV_NONE) {
    static_cast<Board *>(b)->unplug();
    return 0;
  }
  if (n <= 0) return -1;
  Table t;
  t.n = n;
  size_t width = kind == DEV_SPI ? 256 : 512;
  fill(t.out, out, n);
  fill(t.mask, mask, n);
  fill(t.next, next, n * width);
  fill(t.ack, ack, n * width);
  fill(t.a, a, n);
  fill(t.b, bb, n);
  fill(t.c, c, n);
  fill(t.idle_next, idle_next, n);
  Quirks q;
  q.flip_ppm = flip_ppm;
  q.seed = seed;
  q.timeout = idle_next ? timeout : 0;
  q.keeps_state = keeps_state;
  static_cast<Board *>(b)->plug(kind, delay, t, q);
  return 0;
}
int brd_load(void *b, const uint8_t *image, int n) { return static_cast<Board *>(b)->load(image, n); }
int brd_exchange(void *b, const uint8_t *tx, int ntx, uint8_t *rx, int nrx, uint8_t *rx2, int cap2, int *n2,
                 uint64_t limit) {
  return static_cast<Board *>(b)->exchange(tx, ntx, rx, nrx, rx2, cap2, n2, limit);
}
void brd_far(void *b, uint8_t mask, uint8_t val) { static_cast<Board *>(b)->far(mask, val); }
void brd_rise(void *b, uint8_t mask, int clocks) { static_cast<Board *>(b)->set_rise(mask, clocks); }
void brd_run(void *b, uint64_t n) { static_cast<Board *>(b)->run(n); }
void brd_get(void *b, uint64_t *out) { static_cast<Board *>(b)->get(out); }
void brd_clear(void *b) { static_cast<Board *>(b)->clear_faults(); }
void brd_log(void *b, int on) { static_cast<Board *>(b)->log(on); }
size_t brd_records(void *b, const void **p) {
  const auto &r = static_cast<Board *>(b)->records();
  *p = r.data();
  return r.size();
}
size_t brd_save(void *b, const uint8_t **p) {
  const auto &v = static_cast<Board *>(b)->save();
  *p = v.data();
  return v.size();
}
void brd_restore(void *b, const uint8_t *p, size_t n) { static_cast<Board *>(b)->restore(p, n); }
}
