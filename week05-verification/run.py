#!/usr/bin/env python3
# Build and run the cocotb testbench. No chamber, no xrun: Icarus does the
# simulation, cocotb connects your Python to it.
#
# python3 run.py tests your rtl/pe.sv
# python3 run.py vendor tests rtl/pe_vendor_drop.sv
import sys
from pathlib import Path
from cocotb_tools.runner import get_runner
VENDOR = len(sys.argv) > 1 and sys.argv[1] == "vendor"
RTL = Path(__file__).parent.resolve() / ".." / "rtl"
SOURCES = [RTL / "fxp.sv", RTL / ("pe_vendor_drop.sv" if VENDOR else "pe.sv")]
TOPLEVEL = "pe_vendor_drop" if VENDOR else "pe"
runner = get_runner("icarus")
runner.build(sources=SOURCES, hdl_toplevel=TOPLEVEL, always=True, waves=True)
runner.test(hdl_toplevel=TOPLEVEL, test_module="test_pe", waves=True)