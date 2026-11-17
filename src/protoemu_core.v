/*
 * Copyright (c) 2026 Mike Kovetsky
 * SPDX-License-Identifier: Apache-2.0
 *
 * protoemu_core: the table machine of bakeoff/cand_table, clock for clock.
 *
 * The golden model is bakeoff/cand_table/chip.py. One rising edge here is one `TableChip.step`, and every
 * register below has a twin there:
 *
 *   model                  here
 *   mach[m].state          r_st[m]       the SRAM address the machine reads on its next turn
 *   mach[m].link .. R      r_link .. r_R
 *   mach[m].crc, crc_ok    r_crc, r_crcok
 *   mach[m].prev_a         r_preva
 *   T == 0, K == 0, ..     r_tz, r_kz, r_jz, r_nz: the zero flags, kept beside their registers
 *   out, inp, msb          r_omap, r_imap, r_msb (written by the loader)
 *   poly, init, match      r_poly, r_init, r_match (written by the loader)
 *   out, oe                pin_out, pin_oe: the pad output registers
 *   fifo[0..3]             the four protoemu_fifo instances
 *   slot, q_valid, nm      slot, q_valid, nm
 *   loaded                 running
 *   overflow (a count)     overflow (sticky: set by the first dropped push)
 *
 * The host side of the FIFOs keeps the model's order inside one clock: the machine's pop or push of that
 * clock happens first, then the host's. So `h_room0` answers `host_can_push(0)` as asked after `step`, and
 * `h_q1` is what `host_pop(1)` would return then, including a byte the machine pushes in this clock.
 *
 * The loader takes the image format of bakeoff/cand_table/entry.py one byte at a time: the machine count,
 * 16 configuration bytes per machine, the entry count (2 bytes), then 8 bytes per entry from address 0. It
 * writes each entry byte straight into its byte lane of the SRAMs with the bit mask, and then writes zeros
 * to every entry above the count, so the whole table is defined (the model's reset clears it). The
 * machines start on the clock after the last zero is written.
 */

`default_nettype none

module protoemu_core (
    input  wire       clk,
    input  wire       rst_n,

    // Protocol pins P0..P7, already through the synchronisers.
    input  wire [7:0] pins,
    output reg  [7:0] pin_out,
    output reg  [7:0] pin_oe,

    // Loader: one image byte per clock at most, taken while ld_ready is high.
    input  wire       ld_valid,
    input  wire [7:0] ld_byte,
    output wire       ld_ready,
    output reg        running,

    // Host side of the four byte FIFOs, in the model's order (see above).
    input  wire       h_push0,
    input  wire [7:0] h_data0,
    output wire       h_room0,
    input  wire       h_push3,
    input  wire [7:0] h_data3,
    output wire       h_room3,
    input  wire       h_pop1,
    output wire       h_has1,
    output wire [7:0] h_q1,
    input  wire       h_pop2,
    output wire       h_has2,
    output wire [7:0] h_q2,

    // The same FIFOs as they stand in their registers, for the pin host link.
    output wire       st_room0,
    output wire       st_room3,
    output wire       st_has1,
    output wire       st_has2,
    output wire [7:0] st_head1,
    output wire [7:0] st_head2,

    output reg        overflow
);

  // ---- the table: two 512x32 SRAM macros side by side ------------------------------------------------

  wire        ram_men;
  wire        ram_wen;
  wire [8:0]  ram_addr;
  wire [63:0] ram_din;
  wire [63:0] ram_bm;
  wire [63:0] ram_dout;

  protoemu_table table_ram (
      .clk (clk),
      .men (ram_men),
      .wen (ram_wen),
      .ren (running),
      .addr(ram_addr),
      .din (ram_din),
      .bm  (ram_bm),
      .dout(ram_dout)
  );

  // ---- run control -------------------------------------------------------------------------------------

  reg  [1:0] slot;     // whose turn it is
  reg  [2:0] nm;       // machine count, 0 to 4 (0 runs like 1, as in the model)
  reg        q_valid;  // the SRAM outputs hold an entry: false until the first read after loading
  wire       step = running & q_valid;

  // ---- per-machine registers, four of each, packed -----------------------------------------------------

  reg [4*9-1:0]  r_st;
  reg [4*9-1:0]  r_link;
  reg [4*16-1:0] r_T;
  reg [4*16-1:0] r_N;
  reg [4*4-1:0]  r_K;
  reg [4*4-1:0]  r_J;
  reg [4*8-1:0]  r_S;
  reg [4*8-1:0]  r_R;
  reg [4*32-1:0] r_crc;
  reg [3:0]      r_crcok;
  reg [3:0]      r_preva;
  reg [3:0]      r_tz;
  reg [3:0]      r_kz;
  reg [3:0]      r_jz;
  reg [3:0]      r_nz;
  reg [4*12-1:0] r_omap;   // logical outputs 0..3 -> pin, 3 bits each
  reg [4*9-1:0]  r_imap;   // inputs A, B, C -> pin, 3 bits each
  reg [3:0]      r_msb;
  reg [4*32-1:0] r_poly;
  reg [4*32-1:0] r_init;
  reg [4*32-1:0] r_match;

  // The machine whose turn it is.
  wire [8:0]  m_link  = r_link[slot*9+:9];
  wire [15:0] m_T     = r_T[slot*16+:16];
  wire [15:0] m_N     = r_N[slot*16+:16];
  wire [3:0]  m_K     = r_K[slot*4+:4];
  wire [3:0]  m_J     = r_J[slot*4+:4];
  wire [7:0]  m_S     = r_S[slot*8+:8];
  wire [7:0]  m_R     = r_R[slot*8+:8];
  wire [31:0] m_crc   = r_crc[slot*32+:32];
  wire        m_crcok = r_crcok[slot];
  wire        m_preva = r_preva[slot];
  wire        m_tz    = r_tz[slot];
  wire        m_kz    = r_kz[slot];
  wire        m_jz    = r_jz[slot];
  wire        m_nz    = r_nz[slot];
  wire [11:0] m_omap  = r_omap[slot*12+:12];
  wire [8:0]  m_imap  = r_imap[slot*9+:9];
  wire        m_msb   = r_msb[slot];
  wire [31:0] m_poly  = r_poly[slot*32+:32];
  wire [31:0] m_init  = r_init[slot*32+:32];
  wire [31:0] m_match = r_match[slot*32+:32];

  // ---- the entry on the SRAM outputs (bakeoff/cand_table/entry.py) -------------------------------------

  wire [31:0] wa = ram_dout[31:0];
  wire [31:0] wb = ram_dout[63:32];

  wire [8:0]  f_base  = wa[8:0];
  wire [3:0]  f_ca    = wa[12:9];
  wire [3:0]  f_cb    = wa[16:13];
  wire [1:0]  f_p0    = wa[18:17];
  wire [2:0]  f_p0op  = wa[21:19];
  wire [1:0]  f_p1    = wa[23:22];
  wire [2:0]  f_p1op  = wa[26:24];
  wire [1:0]  f_gate  = wa[28:27];
  wire        f_dsel  = wa[29];
  wire [1:0]  f_din   = wa[31:30];
  wire [15:0] f_imm   = wb[15:0];
  wire [2:0]  f_src   = wb[18:16];
  wire [2:0]  f_dst   = wb[21:19];
  wire        f_shift = wb[22];
  wire [2:0]  f_crc   = wb[25:23];
  wire [1:0]  f_nop   = wb[27:26];
  wire [2:0]  f_kj    = wb[30:28];
  wire        f_tload = wb[31];

  // ---- FIFOs: 0 and 3 host -> chip, 1 and 2 chip -> host ----------------------------------------------

  wire [2:0] cnt0, cnt1, cnt2, cnt3;
  wire [7:0] head0, head1, head2, head3;
  reg  [7:0] bus;

  wire m_pop0  = step & (f_src == 3'd1) & (cnt0 != 3'd0);
  wire m_pop3  = step & (f_src == 3'd2) & (cnt3 != 3'd0);
  wire m_push1 = step & (f_dst == 3'd5);
  wire m_push2 = step & (f_dst == 3'd6);
  wire acc1    = m_push1 & (cnt1 != 3'd4);
  wire acc2    = m_push2 & (cnt2 != 3'd4);

  assign h_room0 = (cnt0 != 3'd4) | m_pop0;
  assign h_room3 = (cnt3 != 3'd4) | m_pop3;
  assign h_has1  = (cnt1 != 3'd0) | acc1;
  assign h_has2  = (cnt2 != 3'd0) | acc2;
  assign h_q1    = (cnt1 != 3'd0) ? head1 : bus;
  assign h_q2    = (cnt2 != 3'd0) ? head2 : bus;

  assign st_room0 = cnt0 != 3'd4;
  assign st_room3 = cnt3 != 3'd4;
  assign st_has1  = cnt1 != 3'd0;
  assign st_has2  = cnt2 != 3'd0;
  assign st_head1 = head1;
  assign st_head2 = head2;

  protoemu_fifo fifo0 (
      .clk (clk),
      .rst_n(rst_n),
      .push(h_push0 & h_room0),
      .din (h_data0),
      .pop (m_pop0),
      .cnt (cnt0),
      .head(head0)
  );
  protoemu_fifo fifo1 (
      .clk (clk),
      .rst_n(rst_n),
      .push(acc1),
      .din (bus),
      .pop (h_pop1 & h_has1),
      .cnt (cnt1),
      .head(head1)
  );
  protoemu_fifo fifo2 (
      .clk (clk),
      .rst_n(rst_n),
      .push(acc2),
      .din (bus),
      .pop (h_pop2 & h_has2),
      .cnt (cnt2),
      .head(head2)
  );
  protoemu_fifo fifo3 (
      .clk (clk),
      .rst_n(rst_n),
      .push(h_push3 & h_room3),
      .din (h_data3),
      .pop (m_pop3),
      .cnt (cnt3),
      .head(head3)
  );

  // ---- conditions and the next address -----------------------------------------------------------------

  wire in_a = pins[m_imap[2:0]];
  wire in_b = pins[m_imap[5:3]];
  wire in_c = pins[m_imap[8:6]];
  wire d    = f_dsel ? m_crc[0] : (m_msb ? m_S[7] : m_S[0]);
  wire s_eq = m_S == f_imm[7:0];

  wire [15:0] conds = {
    s_eq, cnt2 != 3'd4, cnt1 != 3'd4, cnt3 != 3'd0, cnt0 != 3'd0, m_crcok, d, m_nz,
    m_jz, m_kz, m_tz, in_a ^ m_preva, in_c, in_b, in_a, 1'b0
  };
  wire va = conds[f_ca];
  wire vb = conds[f_cb];

  wire [8:0] link_n  = (f_dst == 3'd7) ? f_imm[8:0] : m_link;
  wire [8:0] state_n = (f_nop == 2'd3) ? link_n : {f_base[8:2], f_base[1] | vb, f_base[0] | va};

  wire [2:0] slot_inc = {1'b0, slot} + 3'd1;
  wire [1:0] nxt      = (slot_inc >= nm) ? 2'd0 : slot_inc[1:0];

  // The SRAMs latch the address of whoever steps next. With one machine that is the state just chosen:
  // the one-clock loop SRAM out -> condition select -> OR -> mux -> SRAM address.
  wire [8:0] run_addr = (q_valid && nxt == slot) ? state_n : r_st[nxt*9+:9];

  // ---- the datapath of the machine whose turn it is ----------------------------------------------------

  always @(*) begin
    case (f_src)
      3'd0:    bus = f_imm[7:0];
      3'd1:    bus = (cnt0 != 3'd0) ? head0 : 8'd0;
      3'd2:    bus = (cnt3 != 3'd0) ? head3 : 8'd0;
      3'd3:    bus = m_S;
      3'd4:    bus = m_R;
      3'd5:    bus = m_N[7:0];
      3'd6:    bus = m_N[15:8];
      default: bus = 8'd0;
    endcase
  end

  // Serial input: 0, 1, input A or input B.
  wire bit_in = f_din[1] ? (f_din[0] ? in_b : in_a) : f_din[0];

  wire [7:0] s_shifted = m_msb ? {m_S[6:0], bit_in} : {bit_in, m_S[7:1]};
  wire [7:0] s1        = f_shift ? s_shifted : m_S;

  // LFSR. Codes 4, 6 and 7 all step with the serial input, as the model's `else` branch does.
  wire        fb       = m_crc[0] ^ ((f_crc == 3'd3) ? d : bit_in);
  wire [31:0] crc_step = {1'b0, m_crc[31:1]} ^ (fb ? m_poly : 32'd0);
  reg  [31:0] crc_n;
  always @(*) begin
    case (f_crc)
      3'd0:    crc_n = m_crc;
      3'd1:    crc_n = 32'd0;
      3'd2:    crc_n = m_init;
      3'd5:    crc_n = {1'b0, m_crc[31:1]};
      default: crc_n = crc_step;
    endcase
  end

  // Counters. kj = 3 * k_op + j_op, each 0 keep, 1 dec, 2 load.
  wire [15:0] n1 = (f_nop == 2'd1) ? m_N - 16'd1 : (f_nop == 2'd2) ? m_N + 16'd1 : m_N;
  wire k_dec  = (f_kj == 3'd3) | (f_kj == 3'd4) | (f_kj == 3'd5);
  wire k_load = (f_kj == 3'd6) | (f_kj == 3'd7);
  wire j_dec  = (f_kj == 3'd1) | (f_kj == 3'd4) | (f_kj == 3'd7);
  wire j_load = (f_kj == 3'd2) | (f_kj == 3'd5);
  wire [3:0] k1 = k_load ? f_imm[11:8] : k_dec ? m_K - 4'd1 : m_K;
  wire [3:0] j1 = j_load ? f_imm[15:12] : j_dec ? m_J - 4'd1 : m_J;
  wire [15:0] t1 = f_tload ? f_imm : (m_tz ? 16'd0 : m_T - 16'd1);

  // Bus destination, after the counters.
  wire [7:0]  s2 = (f_dst == 3'd1) ? bus : s1;
  wire [7:0]  r2 = (f_dst == 3'd2) ? bus : m_R;
  wire [15:0] n2 = (f_dst == 3'd3) ? {8'd0, bus} : (f_dst == 3'd4) ? {bus, n1[7:0]} : n1;

  // Pin operations, p0 then p1, on the shared pad registers. Only they may be gated by condition A.
  wire gate_ok = (f_gate == 2'd0) | ((f_gate == 2'd1) & va) | ((f_gate == 2'd2) & ~va);
  wire [2:0] pin0 = m_omap[f_p0*3+:3];
  wire [2:0] pin1 = m_omap[f_p1*3+:3];
  wire [7:0] sel0 = (step & gate_ok & (f_p0op != 3'd0)) ? (8'd1 << pin0) : 8'd0;
  wire [7:0] sel1 = (step & gate_ok & (f_p1op != 3'd0)) ? (8'd1 << pin1) : 8'd0;

  // One pin operation on one pin: {out, oe} in, {out, oe} out.
  function automatic [1:0] pin_op(input [2:0] op, input o, input e, input dd);
    case (op)
      3'd1:    pin_op = 2'b01;          // 0
      3'd2:    pin_op = 2'b11;          // 1
      3'd3:    pin_op = {o, 1'b0};      // release
      3'd4:    pin_op = {dd, 1'b1};     // data bit
      3'd5:    pin_op = {~dd, 1'b1};    // inverted data bit
      3'd6:    pin_op = {~o, 1'b1};     // toggle
      3'd7:    pin_op = {1'b0, ~dd};    // open drain
      default: pin_op = {o, e};         // keep
    endcase
  endfunction

  reg [7:0] pin_out_n, pin_oe_n;
  reg [1:0] oe_tmp;
  integer i;
  always @(*) begin
    for (i = 0; i < 8; i = i + 1) begin
      oe_tmp = {pin_out[i], pin_oe[i]};
      if (sel0[i]) oe_tmp = pin_op(f_p0op, oe_tmp[1], oe_tmp[0], d);
      if (sel1[i]) oe_tmp = pin_op(f_p1op, oe_tmp[1], oe_tmp[0], d);
      pin_out_n[i] = oe_tmp[1];
      pin_oe_n[i]  = oe_tmp[0];
    end
  end

  // ---- loader ------------------------------------------------------------------------------------------

  localparam [2:0] L_HDR = 3'd0, L_CFG = 3'd1, L_CLO = 3'd2, L_CHI = 3'd3, L_ENT = 3'd4, L_FILL = 3'd5,
                   L_DONE = 3'd6;

  reg  [2:0]  ld_st;
  reg  [12:0] ld_pos;   // L_CFG: {machine, byte}; L_ENT and L_FILL: {entry, byte lane}
  reg  [9:0]  ld_cnt;   // L_CFG: machines in the image; later: entries, at most 512

  assign ld_ready = (ld_st == L_HDR) | (ld_st == L_CFG) | (ld_st == L_CLO) | (ld_st == L_CHI) |
                    (ld_st == L_ENT);
  wire       take    = ld_valid & ld_ready;
  wire       ld_ent  = (ld_st == L_ENT) & take;
  wire       ld_fill = (ld_st == L_FILL) & ~ld_pos[12];
  wire [15:0] ld_count = {ld_byte, ld_cnt[7:0]};
  wire [9:0] ld_next_entry = ld_pos[12:3] + 10'd1;
  wire       cfg_last = (ld_pos[3:0] == 4'd15) & (ld_pos[11:4] == ld_cnt[7:0] - 8'd1);
  wire       cfg_we   = (ld_st == L_CFG) & take & (ld_pos[11:6] == 6'd0);
  wire [1:0] cfg_m    = ld_pos[5:4];
  wire [3:0] cfg_b    = ld_pos[3:0];

  assign ram_men  = running | ld_ent | ld_fill;
  assign ram_wen  = ld_ent | ld_fill;
  assign ram_addr = running ? run_addr : ld_pos[11:3];
  assign ram_din  = ld_ent ? {8{ld_byte}} : 64'd0;
  assign ram_bm   = ld_ent ? (64'hFF << {ld_pos[2:0], 3'b000}) : {64{1'b1}};

  // ---- registers ---------------------------------------------------------------------------------------

  integer m;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      running  <= 1'b0;
      slot     <= 2'd0;
      nm       <= 3'd1;
      q_valid  <= 1'b0;
      pin_out  <= 8'd0;
      pin_oe   <= 8'd0;
      overflow <= 1'b0;
      ld_st    <= L_HDR;
      ld_pos   <= 13'd0;
      ld_cnt   <= 10'd0;
      r_st     <= {9'd3, 9'd2, 9'd1, 9'd0};  // machine m starts in entry m
      r_link   <= 36'd0;
      r_T      <= 64'd0;
      r_N      <= 64'd0;
      r_K      <= 16'd0;
      r_J      <= 16'd0;
      r_S      <= 32'd0;
      r_R      <= 32'd0;
      r_crc    <= 128'd0;
      r_crcok  <= 4'b0000;
      r_preva  <= 4'b1111;
      r_tz     <= 4'b1111;
      r_kz     <= 4'b1111;
      r_jz     <= 4'b1111;
      r_nz     <= 4'b1111;
      r_omap   <= 48'd0;
      r_imap   <= 36'd0;
      r_msb    <= 4'b0000;
      r_poly   <= 128'd0;
      r_init   <= 128'd0;
      r_match  <= 128'd0;
    end else begin
      // One step of the machine whose turn it is.
      if (running) begin
        slot    <= nxt;
        q_valid <= 1'b1;
      end
      if (step) begin
        pin_out  <= pin_out_n;
        pin_oe   <= pin_oe_n;
        overflow <= overflow | (m_push1 & ~acc1) | (m_push2 & ~acc2);
        for (m = 0; m < 4; m = m + 1) begin
          if (slot == m[1:0]) begin
            r_st[m*9+:9]    <= state_n;
            r_link[m*9+:9]  <= link_n;
            r_T[m*16+:16]   <= t1;
            r_N[m*16+:16]   <= n2;
            r_K[m*4+:4]     <= k1;
            r_J[m*4+:4]     <= j1;
            r_S[m*8+:8]     <= s2;
            r_R[m*8+:8]     <= r2;
            r_crc[m*32+:32] <= crc_n;
            r_crcok[m]      <= m_crc == m_match;  // the compare is registered: one turn behind the LFSR
            r_preva[m]      <= in_a;
            r_tz[m]         <= t1 == 16'd0;
            r_kz[m]         <= k1 == 4'd0;
            r_jz[m]         <= j1 == 4'd0;
            r_nz[m]         <= n2 == 16'd0;
          end
        end
      end

      // The loader.
      case (ld_st)
        L_HDR:
        if (take) begin
          nm     <= (ld_byte > 8'd4) ? 3'd4 : ld_byte[2:0];
          ld_cnt <= {2'b00, ld_byte};
          ld_pos <= 13'd0;
          ld_st  <= (ld_byte == 8'd0) ? L_CLO : L_CFG;
        end
        L_CFG:
        if (take) begin
          ld_pos <= ld_pos + 13'd1;
          if (cfg_last) ld_st <= L_CLO;
        end
        L_CLO:
        if (take) begin
          ld_cnt[7:0] <= ld_byte;
          ld_st       <= L_CHI;
        end
        L_CHI:
        if (take) begin
          ld_cnt <= (ld_count > 16'd512) ? 10'd512 : ld_count[9:0];
          ld_pos <= 13'd0;
          ld_st  <= (ld_count == 16'd0) ? L_FILL : L_ENT;
        end
        L_ENT:
        if (take) begin
          ld_pos <= ld_pos + 13'd1;
          if (ld_pos[2:0] == 3'd7 && ld_next_entry == ld_cnt) ld_st <= L_FILL;
        end
        L_FILL:
        if (ld_pos[12]) begin
          ld_st   <= L_DONE;
          running <= 1'b1;
        end else begin
          ld_pos <= ld_pos + 13'd8;
        end
        default: ;
      endcase

      // Configuration bytes, 16 per machine (MachineConfig in entry.py).
      if (cfg_we) begin
        for (m = 0; m < 4; m = m + 1) begin
          if (cfg_m == m[1:0]) begin
            case (cfg_b)
              4'd0: r_omap[m*12+:6] <= ld_byte[5:0];
              4'd1: r_omap[m*12+6+:6] <= ld_byte[5:0];
              4'd2: r_imap[m*9+:6] <= ld_byte[5:0];
              4'd3: begin
                r_imap[m*9+6+:3] <= ld_byte[2:0];
                r_msb[m]         <= ld_byte[3];
              end
              4'd4:  r_poly[m*32+:8] <= ld_byte;
              4'd5:  r_poly[m*32+8+:8] <= ld_byte;
              4'd6:  r_poly[m*32+16+:8] <= ld_byte;
              4'd7:  r_poly[m*32+24+:8] <= ld_byte;
              4'd8:  r_init[m*32+:8] <= ld_byte;
              4'd9:  r_init[m*32+8+:8] <= ld_byte;
              4'd10: r_init[m*32+16+:8] <= ld_byte;
              4'd11: r_init[m*32+24+:8] <= ld_byte;
              4'd12: r_match[m*32+:8] <= ld_byte;
              4'd13: r_match[m*32+8+:8] <= ld_byte;
              4'd14: r_match[m*32+16+:8] <= ld_byte;
              default: r_match[m*32+24+:8] <= ld_byte;
            endcase
          end
        end
      end
    end
  end

endmodule
