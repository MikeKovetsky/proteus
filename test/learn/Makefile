# The learn-and-become tests. They are not in CI: the workflows run `make -C test` only.
#
#   source ~/oss-cad-suite/environment
#   make -C test/learn          every test (a few minutes: it learns and replaces three devices on RTL)
#   make -C test/learn quick    all but the end-to-end test
#   LEARN_JOBS=n                heavy processes at a time, 2 if not given. No result depends on it
#
# Verilator, Icarus and cocotb come with the OSS CAD Suite.

QUICK = test_guarantee test_learner test_table test_generators test_isolation test_lab test_families test_baselines

.PHONY: all quick
all:
	cd $(CURDIR) && python3 -B -m unittest discover -v -p 'test_*.py'

quick:
	cd $(CURDIR) && python3 -B -m unittest -v $(QUICK)
