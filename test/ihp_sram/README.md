# IHP SRAM simulation model

The two `.v` files here are IHP's own simulation model of the `RM_IHPSG13_1P_512x32_c2_bm_bist` SRAM macro,
copied unchanged from [IHP's open PDK](https://github.com/IHP-GmbH/IHP-Open-PDK/tree/main/ihp-sg13g2/libs.ref/sg13g2_sram/verilog)
on 2026-11-16 (Apache 2.0, licence header in each file). The CMOS5L SRAM folder in the PDK is a link to the
same files.

| File | Last changed in the PDK |
|---|---|
| `RM_IHPSG13_1P_512x32_c2_bm_bist.v` | `700c6c7`, 2026-08-11 |
| `RM_IHPSG13_1P_core_behavioral_bm_bist.v` | `9bda069`, 2026-08-11 |

Compile with `-DFUNCTIONAL`: that selects the behavioural core without the timing checks, which Icarus and
Verilator cannot run. The model reads synchronously (the address is latched on the rising edge and the data
holds until the next read), writes through a bit mask, and stops the simulation if `A_DLY` is not tied to 1.
Both Icarus and Verilator (`--no-timing`) run it.
