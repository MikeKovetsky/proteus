// SPDX-FileCopyrightText: © 2026 Mike Kovetsky
// SPDX-License-Identifier: Apache-2.0
`default_nettype none
`timescale 1ns / 1ps

// Testbench wrapper exposing 1-bit protocol wires for independent third-party cocotbext VIPs
// (cocotbext-uart, cocotbext-i2c, cocotbext-spi) against tt_um_mikekovetsky_protoemu.
module tb_oracle ();

  reg clk;
  reg rst_n;
  reg ena;
  reg [7:0] ui_in;
  wire [7:0] uo_out;
  wire [7:0] uio_out;
  wire [7:0] uio_oe;

  // Bus routing mode:
  //   0 = UART (P0=TX out, P1=RX in)
  //   1 = I2C  (P0=SCL open-drain, P1=SDA open-drain)
  //   2 = SPI controller on chip (P0=SCK out, P1=MOSI out, P2=MISO in, P3=CS out)
  //   3 = SPI target on chip (P0=SCK in, P1=MOSI in, P2=MISO out, P3=CS in, P4=nRESET in)
  reg [1:0] bus_mode;

  // UART 1-bit wires
  wire uart_tx = uio_oe[0] ? uio_out[0] : 1'b1;
  reg  uart_rx;

  // I2C open-drain wires (1 = released/pulled high, 0 = pulled low)
  reg  i2c_scl_drv;
  reg  i2c_sda_drv;
  wire i2c_scl = i2c_scl_drv & (~uio_oe[0] | uio_out[0]);
  wire i2c_sda = i2c_sda_drv & (~uio_oe[1] | uio_out[1]);

  // SPI controller-on-chip wires (chip drives SCK/MOSI/CS, cocotbext slave drives MISO)
  wire spi_c_sclk = uio_oe[0] ? uio_out[0] : 1'b0;
  wire spi_c_mosi = uio_oe[1] ? uio_out[1] : 1'b0;
  reg  spi_c_miso;
  wire spi_c_cs   = uio_oe[3] ? uio_out[3] : 1'b1;

  // SPI target-on-chip wires (cocotbext master drives SCK/MOSI/CS, chip drives MISO)
  reg  spi_t_sclk;
  reg  spi_t_mosi;
  wire spi_t_miso = uio_oe[2] ? uio_out[2] : 1'b1;
  reg  spi_t_cs;
  reg  spi_t_nrst;

  reg [7:0] uio_in_mux;
  always @(*) begin
    case (bus_mode)
      2'd0: uio_in_mux = {6'b111111, uart_rx, uart_tx};
      2'd1: uio_in_mux = {6'b111111, i2c_sda, i2c_scl};
      2'd2: uio_in_mux = {4'b1111, spi_c_cs, spi_c_miso, spi_c_mosi, spi_c_sclk};
      2'd3: uio_in_mux = {3'b111, spi_t_nrst, spi_t_cs, spi_t_miso, spi_t_mosi, spi_t_sclk};
    endcase
  end

  tt_um_mikekovetsky_protoemu user_project (
      .ui_in  (ui_in),
      .uo_out (uo_out),
      .uio_in (uio_in_mux),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (ena),
      .clk    (clk),
      .rst_n  (rst_n)
  );

  initial begin
    bus_mode    = 2'd0;
    uart_rx     = 1'b1;
    i2c_scl_drv = 1'b1;
    i2c_sda_drv = 1'b1;
    spi_c_miso  = 1'b1;
    spi_t_sclk  = 1'b0;
    spi_t_mosi  = 1'b0;
    spi_t_cs    = 1'b1;
    spi_t_nrst  = 1'b1;
  end

endmodule
