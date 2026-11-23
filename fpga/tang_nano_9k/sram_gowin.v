/*
 * Copyright (c) 2026 Mike Kovetsky
 * SPDX-License-Identifier: Apache-2.0
 *
 * Gowin BSRAM drop-in implementation of the IHP SG13 512x32 single-port SRAM macro
 * (`RM_IHPSG13_1P_512x32_c2_bm_bist`) so `src/proteus_table.v` synthesizes unmodified
 * onto the Sipeed Tang Nano 9K (GW1NR-LV9QN88PC6/I5).
 *
 * `proteus_core` writes one byte lane at a time (A_BM is byte-aligned) and performs
 * synchronous reads when A_MEN && A_REN. Splitting into four 512x8 byte lanes lets
 * Yosys `synth_gowin` infer hard Gowin BSRAM blocks with exact byte-enable behavior.
 */

`default_nettype none

/* verilator lint_off DECLFILENAME */
/* verilator lint_off UNUSEDSIGNAL */
module RM_IHPSG13_1P_512x32_c2_bm_bist (
    input  wire        A_CLK,
    input  wire        A_MEN,
    input  wire        A_WEN,
    input  wire        A_REN,
    input  wire [ 8:0] A_ADDR,
    input  wire [31:0] A_DIN,
    input  wire        A_DLY,
    output wire [31:0] A_DOUT,
    input  wire [31:0] A_BM,
    input  wire        A_BIST_CLK,
    input  wire        A_BIST_EN,
    input  wire        A_BIST_MEN,
    input  wire        A_BIST_WEN,
    input  wire        A_BIST_REN,
    input  wire [ 8:0] A_BIST_ADDR,
    input  wire [31:0] A_BIST_DIN,
    input  wire [31:0] A_BIST_BM
);

  reg [7:0] mem0 [0:511];
  reg [7:0] mem1 [0:511];
  reg [7:0] mem2 [0:511];
  reg [7:0] mem3 [0:511];

  reg [7:0] q0, q1, q2, q3;

  wire we0 = A_MEN & A_WEN & A_BM[0];
  wire we1 = A_MEN & A_WEN & A_BM[8];
  wire we2 = A_MEN & A_WEN & A_BM[16];
  wire we3 = A_MEN & A_WEN & A_BM[24];
  wire re  = A_MEN & A_REN;

  always @(posedge A_CLK) begin
    if (we0) mem0[A_ADDR] <= A_DIN[7:0];
    if (re)  q0 <= we0 ? A_DIN[7:0] : mem0[A_ADDR];
  end

  always @(posedge A_CLK) begin
    if (we1) mem1[A_ADDR] <= A_DIN[15:8];
    if (re)  q1 <= we1 ? A_DIN[15:8] : mem1[A_ADDR];
  end

  always @(posedge A_CLK) begin
    if (we2) mem2[A_ADDR] <= A_DIN[23:16];
    if (re)  q2 <= we2 ? A_DIN[23:16] : mem2[A_ADDR];
  end

  always @(posedge A_CLK) begin
    if (we3) mem3[A_ADDR] <= A_DIN[31:24];
    if (re)  q3 <= we3 ? A_DIN[31:24] : mem3[A_ADDR];
  end

  assign A_DOUT = {q3, q2, q1, q0};

endmodule
/* verilator lint_on UNUSEDSIGNAL */
/* verilator lint_on DECLFILENAME */

`default_nettype wire
