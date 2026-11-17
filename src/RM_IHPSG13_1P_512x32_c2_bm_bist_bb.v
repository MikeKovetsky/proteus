/*
 * Black box of the IHP SG13 single-port 512 x 32 SRAM macro, for the flow only.
 *
 * src/config.json names this file as the macro's "vh" view, so LibreLane's Verilator lint and Yosys synthesis
 * see the ports and nothing else. Simulation never reads it: test/Makefile uses the PDK's behavioural model in
 * test/ihp_sram/ instead. Do not list this file in info.yaml's source_files.
 *
 * Ports as in the PDK's libs.ref/sg13g2_sram/verilog/RM_IHPSG13_1P_512x32_c2_bm_bist.v (IHP-Open-PDK
 * 89d42ee6d2fd, Apache-2.0, "Copyright 2023 IHP PDK Authors"), which sg13cmos5l_sram links to.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

/* verilator lint_off DECLFILENAME */
/* verilator lint_off UNDRIVEN */
/* verilator lint_off UNUSEDSIGNAL */
(* blackbox *)
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
endmodule
/* verilator lint_on UNUSEDSIGNAL */
/* verilator lint_on UNDRIVEN */
/* verilator lint_on DECLFILENAME */

`default_nettype wire
