// SPDX-FileCopyrightText: © 2026 Mike Kovetsky
// SPDX-License-Identifier: Apache-2.0
//
// Cycle-accurate Verilator pin-level runner for Proteus Inline Hardware Risk Gate.
// Drives the real Tiny Tapeout 24-pin interface (ui_in, uo_out, uio_in, uio_out, uio_oe)
// of unmodified `tt_um_mikekovetsky_proteus` at 60 MHz (16.667 ns/clock).
//
// Communicates via newline-delimited JSON commands on stdin/stdout so Python / Qubx /
// interactive TUI can stream real binary UART order frames through the actual Verilog,
// pull physical P4 (KILL_N) at any exact clock cycle, and read back decoded TX wire
// frames, Host Channel 1 audit events, and Port-3 hardware logic-analyzer waveforms.

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#include "Vtt_um_mikekovetsky_proteus.h"
#include "verilated.h"

static constexpr int PORT_CH0_CH1 = 0;
static constexpr int PORT_CH3_CH2 = 1;
static constexpr int PORT_LOADER  = 2;
static constexpr int PORT_TRACE   = 3;

static constexpr uint8_t ST_ROOM     = 0x10;
static constexpr uint8_t ST_HAS      = 0x20;
static constexpr uint8_t ST_RUNNING  = 0x40;
static constexpr uint8_t ST_OVERFLOW = 0x80;

// 3.000 Mbaud @ 60 MHz = 20 clocks per bit (matches BIT=10 turns * 2 clocks/turn).
static constexpr int BIT_CLKS = 20;

struct TraceSample {
    uint64_t clk;
    uint8_t  pins;
    uint8_t  delta;
};

struct ProteusSim {
    Vtt_um_mikekovetsky_proteus dut;
    uint64_t clk_count = 0;

    // Host nibble-link state
    uint8_t h_nib  = 0;
    uint8_t h_port = 0;
    uint8_t h_wr   = 0;
    uint8_t h_rd   = 0;

    // External pad drivers (P0=RX_IN idle 1, P4=KILL_N default armed 1)
    uint8_t ext_rx   = 1;
    uint8_t ext_kill = 1;

    // UART TX wire decoder (snooping P1 = TX_OUT on every clock)
    enum RxState { U_IDLE, U_START, U_DATA, U_STOP };
    RxState tx_dec_state = U_IDLE;
    int     tx_dec_cnt   = 0;
    int     tx_dec_bit   = 0;
    uint8_t tx_dec_byte  = 0;
    uint64_t tx_first_edge_clk = 0;
    bool     tx_saw_edge       = false;
    std::vector<uint8_t> tx_decoded_bytes;
    bool     tx_framing_abort  = false;

    // Raw cycle-exact waveform ring (for full clarity alongside Port-3 HW trace)
    uint8_t  last_eff_pins = 0xFF;
    uint64_t last_rx_edge_clk = 0;

    ProteusSim() {
        dut.ena    = 1;
        dut.clk    = 0;
        dut.rst_n  = 0;
        dut.ui_in  = 0;
        dut.uio_in = 0xFF;
    }

    uint8_t effective_pins() const {
        uint8_t oe  = dut.uio_oe & 0xFF;
        uint8_t out = dut.uio_out & 0xFF;
        uint8_t in  = dut.uio_in & 0xFF;
        return (oe & out) | ((~oe) & in);
    }

    void update_pads() {
        dut.ui_in = (h_nib & 0x0F) | ((h_port & 0x03) << 4) | ((h_wr & 1) << 6) | ((h_rd & 1) << 7);
        // External pins: P0=RX_IN, P4=KILL_N; all other undriven pins weakly pulled up,
        // and any chip-driven pin loops its driven level back onto uio_in (like a real pad!).
        uint8_t ext = 0xFF;
        if (!ext_rx)   ext &= ~(1u << 0);
        if (!ext_kill) ext &= ~(1u << 4);
        uint8_t oe  = dut.uio_oe & 0xFF;
        uint8_t out = dut.uio_out & 0xFF;
        dut.uio_in  = (oe & out) | ((~oe) & ext);
    }

    void step_uart_tx_decoder() {
        uint8_t eff = effective_pins();
        uint8_t tx  = (eff >> 1) & 1;  // P1 = TX_OUT

        switch (tx_dec_state) {
            case U_IDLE:
                if (tx == 0) {
                    if (!tx_saw_edge) {
                        tx_first_edge_clk = clk_count;
                        tx_saw_edge = true;
                    }
                    tx_dec_state = U_START;
                    tx_dec_cnt   = BIT_CLKS / 2;  // mid start bit
                }
                break;
            case U_START:
                if (--tx_dec_cnt == 0) {
                    if (tx == 0) {
                        tx_dec_state = U_DATA;
                        tx_dec_cnt   = BIT_CLKS;
                        tx_dec_bit   = 0;
                        tx_dec_byte  = 0;
                    } else {
                        // Start bit aborted (e.g. mid-TX kill-switch!)
                        tx_framing_abort = true;
                        tx_dec_state = U_IDLE;
                    }
                }
                break;
            case U_DATA:
                if (--tx_dec_cnt == 0) {
                    tx_dec_byte |= (uint8_t)(tx << tx_dec_bit);
                    tx_dec_bit++;
                    if (tx_dec_bit == 8) {
                        tx_dec_state = U_STOP;
                        tx_dec_cnt   = BIT_CLKS;
                    } else {
                        tx_dec_cnt = BIT_CLKS;
                    }
                }
                break;
            case U_STOP:
                if (--tx_dec_cnt == 0) {
                    if (tx == 1) {
                        tx_decoded_bytes.push_back(tx_dec_byte);
                    } else {
                        tx_framing_abort = true;
                    }
                    tx_dec_state = U_IDLE;
                }
                break;
        }
    }

    void tick(int n = 1) {
        for (int i = 0; i < n; i++) {
            update_pads();
            dut.clk = 1;
            dut.eval();
            update_pads();
            dut.eval();
            clk_count++;
            step_uart_tx_decoder();

            dut.clk = 0;
            dut.eval();
            update_pads();
            dut.eval();
        }
    }

    void reset() {
        h_nib = h_port = h_wr = h_rd = 0;
        ext_rx = 1;
        ext_kill = 1;
        tx_dec_state = U_IDLE;
        tx_decoded_bytes.clear();
        dut.rst_n = 0;
        tick(12);
        dut.rst_n = 1;
        tick(6);
    }

    uint8_t status() const { return dut.uo_out & 0xFF; }

    void host_select(int port) {
        if (h_port != port) {
            h_port = (uint8_t)port;
            tick(4);
        }
    }

    bool host_wait(uint8_t mask, int limit = 50000) {
        for (int i = 0; i < limit; i++) {
            if (status() & mask) return true;
            tick(1);
        }
        return false;
    }

    void host_write_byte(int port, uint8_t b) {
        host_select(port);
        for (int k = 0; k < 2; k++) {
            h_nib = (k == 0) ? (b & 0x0F) : ((b >> 4) & 0x0F);
            tick(1);
            h_wr ^= 1;
            tick(4);
        }
    }

    bool host_try_read_byte(int port, uint8_t &out_b) {
        host_select(port);
        if (!(status() & ST_HAS)) return false;
        uint8_t lo = status() & 0x0F;
        h_rd ^= 1;
        tick(4);
        uint8_t hi = status() & 0x0F;
        h_rd ^= 1;
        tick(4);
        out_b = lo | (hi << 4);
        return true;
    }

    bool load_image(const std::vector<uint8_t> &img) {
        for (uint8_t b : img) {
            host_select(PORT_LOADER);
            if (!host_wait(ST_ROOM, 10000)) return false;
            host_write_byte(PORT_LOADER, b);
        }
        return host_wait(ST_RUNNING, 2000);
    }

    // Arm Port-3 hardware pin-change trace buffer with mask=0x1F (P0..P4).
    void trace_arm(uint8_t mask = 0x1F) {
        host_write_byte(PORT_TRACE, 0x03);
        host_write_byte(PORT_TRACE, mask);
        host_write_byte(PORT_TRACE, 0x02);  // clear & arm trigger on first change
    }

    void trace_start_now(uint8_t mask = 0x1F) {
        host_write_byte(PORT_TRACE, 0x03);
        host_write_byte(PORT_TRACE, mask);
        host_write_byte(PORT_TRACE, 0x01);  // clear & start immediately
    }

    std::vector<std::pair<uint8_t, uint8_t>> trace_drain() {
        host_write_byte(PORT_TRACE, 0x00);  // stop recording
        std::vector<std::pair<uint8_t, uint8_t>> evs;
        uint8_t pins_b = 0, delta_b = 0;
        while (host_try_read_byte(PORT_TRACE, pins_b)) {
            if (!host_try_read_byte(PORT_TRACE, delta_b)) break;
            evs.push_back({pins_b, delta_b});
        }
        return evs;
    }

    void send_uart_byte(uint8_t b, int kill_at_bit = -1) {
        // Start bit (0)
        ext_rx = 0;
        last_rx_edge_clk = clk_count;
        tick(BIT_CLKS);
        // 8 data bits LSB-first
        for (int i = 0; i < 8; i++) {
            ext_rx = (b >> i) & 1;
            if (kill_at_bit == i) ext_kill = 0;
            tick(BIT_CLKS);
        }
        // Stop bit (1)
        ext_rx = 1;
        last_rx_edge_clk = clk_count;
        tick(BIT_CLKS);
    }
};

static std::vector<uint8_t> hex_to_bytes(const std::string &hex) {
    std::vector<uint8_t> out;
    for (size_t i = 0; i + 1 < hex.size(); i += 2) {
        uint8_t hi = (uint8_t)strtoul(hex.substr(i, 2).c_str(), nullptr, 16);
        out.push_back(hi);
    }
    return out;
}

static std::string bytes_to_hex(const std::vector<uint8_t> &v) {
    static const char *H = "0123456789abcdef";
    std::string s;
    s.reserve(v.size() * 2);
    for (uint8_t b : v) {
        s.push_back(H[b >> 4]);
        s.push_back(H[b & 0x0F]);
    }
    return s;
}

int main(int argc, char **argv) {
    Verilated::commandArgs(argc, argv);
    ProteusSim sim;

    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) continue;
        std::istringstream iss(line);
        std::string cmd;
        iss >> cmd;

        if (cmd == "QUIT") {
            std::cout << "{\"ok\":true}\n" << std::flush;
            break;
        } else if (cmd == "LOAD") {
            std::string hex;
            iss >> hex;
            auto img = hex_to_bytes(hex);
            sim.reset();
            bool ok = sim.load_image(img);
            sim.tick(40);
            std::cout << "{\"ok\":" << (ok ? "true" : "false")
                      << ",\"running\":" << ((sim.status() & ST_RUNNING) ? "true" : "false")
                      << ",\"overflow\":" << ((sim.status() & ST_OVERFLOW) ? "true" : "false")
                      << ",\"clk\":" << sim.clk_count << "}\n" << std::flush;
        } else if (cmd == "SET_KILL") {
            int armed = 1;
            iss >> armed;
            sim.ext_kill = armed ? 1 : 0;
            sim.tick(12);
            uint8_t eff = sim.effective_pins();
            std::cout << "{\"ok\":true,\"kill_n\":" << (int)sim.ext_kill
                      << ",\"alert_kill\":" << (int)((eff >> 3) & 1)
                      << ",\"alert_rl\":" << (int)((eff >> 2) & 1)
                      << ",\"clk\":" << sim.clk_count << "}\n" << std::flush;
        } else if (cmd == "ADVANCE") {
            int clks = 0;
            iss >> clks;
            sim.tick(clks);
            uint8_t eff = sim.effective_pins();
            std::cout << "{\"ok\":true,\"clk\":" << sim.clk_count
                      << ",\"pins\":" << (int)eff << "}\n" << std::flush;
        } else if (cmd == "SEND_FRAME") {
            // SEND_FRAME <8-hex-chars> [kill_mid_tx_clks] [arm_trace]
            std::string hex;
            int kill_mid_tx_clks = -1;
            int arm_trace = 0;
            iss >> hex;
            if (iss >> kill_mid_tx_clks) {
                iss >> arm_trace;
            }
            auto frame = hex_to_bytes(hex);

            // Reset TX decoder for this frame
            sim.tx_decoded_bytes.clear();
            sim.tx_saw_edge = false;
            sim.tx_first_edge_clk = 0;
            sim.tx_framing_abort = false;

            // Drain any stale audit bytes first
            uint8_t tmp = 0;
            while (sim.host_try_read_byte(PORT_CH0_CH1, tmp)) {}

            if (arm_trace) {
                sim.trace_arm(0x1F);
            }

            for (size_t i = 0; i < frame.size(); i++) {
                sim.send_uart_byte(frame[i]);
            }
            uint64_t rx_end_clk = sim.clk_count;

            // Let Proteus decide + forward (4 bytes * 10 bits * 20 clks = 800 clks + margin).
            // If kill_mid_tx_clks >= 0, yank physical P4 (KILL_N) low mid-transmission!
            uint64_t kill_assert_clk = 0;
            uint64_t tx_cut_clk = 0;
            if (kill_mid_tx_clks >= 0) {
                // Wait for TX start bit to appear, then capture active TX bits right across the kill edge!
                for (int w = 0; w < 400 && !sim.tx_saw_edge; w++) sim.tick(1);
                int pre_trace = (kill_mid_tx_clks > 80) ? (kill_mid_tx_clks - 80) : 0;
                int post_arm  = kill_mid_tx_clks - pre_trace;
                if (pre_trace > 0) sim.tick(pre_trace);
                if (arm_trace == 2) {
                    // Start Port-3 hardware trace ~80 clocks BEFORE pulling kill-switch so the
                    // waveform shows active TX bits, the exact P4 falling edge, and the instant cut!
                    sim.trace_start_now(0x1F);
                }
                if (post_arm > 0) sim.tick(post_arm);
                sim.ext_kill = 0;
                kill_assert_clk = sim.clk_count;
                // Watch P1 (TX) & P3 (ALERT_KILL) immediately after P4 falls
                for (int w = 0; w < 900; w++) {
                    sim.tick(1);
                    uint8_t eff = sim.effective_pins();
                    if (tx_cut_clk == 0 && ((eff >> 3) & 1)) {
                        tx_cut_clk = sim.clk_count;
                        if (arm_trace == 2) {
                            // Capture ~40 clocks after cut so Port-3 zoom frames the kill edge dead-center!
                            sim.tick(40);
                            break;
                        }
                    }
                }
            } else {
                sim.tick(960);
            }

            // Read hardware audit bytes pushed to Host Channel 1
            std::vector<uint8_t> audits;
            while (sim.host_try_read_byte(PORT_CH0_CH1, tmp)) {
                audits.push_back(tmp);
            }

            // Read Port-3 hardware trace buffer if armed
            std::vector<std::pair<uint8_t, uint8_t>> trace_evs;
            if (arm_trace) {
                trace_evs = sim.trace_drain();
            }

            int64_t lat_clks = sim.tx_saw_edge ? (int64_t)(sim.tx_first_edge_clk - rx_end_clk) : -1;
            int64_t kill_resp_clks = (kill_assert_clk && tx_cut_clk) ? (int64_t)(tx_cut_clk - kill_assert_clk) : -1;
            uint8_t eff = sim.effective_pins();

            std::cout << "{\"ok\":true"
                      << ",\"rx_hex\":\"" << hex << "\""
                      << ",\"tx_hex\":\"" << bytes_to_hex(sim.tx_decoded_bytes) << "\""
                      << ",\"audits_hex\":\"" << bytes_to_hex(audits) << "\""
                      << ",\"lat_clks\":" << lat_clks
                      << ",\"kill_resp_clks\":" << kill_resp_clks
                      << ",\"tx_abort\":" << (sim.tx_framing_abort ? "true" : "false")
                      << ",\"alert_rl\":" << (int)((eff >> 2) & 1)
                      << ",\"alert_kill\":" << (int)((eff >> 3) & 1)
                      << ",\"overflow\":" << ((sim.status() & ST_OVERFLOW) ? "true" : "false")
                      << ",\"trace\":[";
            for (size_t i = 0; i < trace_evs.size(); i++) {
                if (i) std::cout << ",";
                std::cout << "[" << (int)trace_evs[i].first << "," << (int)trace_evs[i].second << "]";
            }
            std::cout << "]}\n" << std::flush;
        } else {
            std::cout << "{\"ok\":false,\"error\":\"unknown cmd\"}\n" << std::flush;
        }
    }
    return 0;
}
