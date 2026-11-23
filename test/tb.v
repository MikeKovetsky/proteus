`default_nettype none
`timescale 1ns / 1ps

/* This testbench just instantiates the module and makes some convenient wires
   that can be driven / tested by the cocotb test.py.
*/
module tb ();

  // Dump signals to an FST file. Gate-level simulation has ~11k cells, so dump only top-level
  // pins during GL_TEST unless -DDUMP_ALL is set (2-3x faster gl_test). Skip when -DNODUMP or
  // under Verilator (pass WAVES=1 to cocotb if traces are needed).
`ifndef VERILATOR
`ifndef NODUMP
  initial begin
    $dumpfile("tb.fst");
`ifdef GL_TEST
`ifdef DUMP_ALL
    $dumpvars(0, tb);
`else
    $dumpvars(1, tb);
`endif
`else
    $dumpvars(0, tb);
`endif
    #1;
  end
`endif
`endif

  // Wire up the inputs and outputs:
  reg clk;
  reg rst_n;
  reg ena;
  reg [7:0] ui_in;
  reg [7:0] uio_in;
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;

  // The design under test:
  tt_um_mikekovetsky_proteus user_project (
      .ui_in  (ui_in),    // Dedicated inputs
      .uo_out (uo_out),   // Dedicated outputs
      .uio_in (uio_in),   // IOs: Input path
      .uio_out(uio_out),  // IOs: Output path
      .uio_oe (uio_oe),   // IOs: Enable path (active high: 0=input, 1=output)
      .ena    (ena),      // enable - goes high when design is selected
      .clk    (clk),      // clock
      .rst_n  (rst_n)     // not reset
  );

endmodule
