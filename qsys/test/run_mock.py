#!/usr/bin/env python3
"""Run a radio.lua scenario against MockCore (needs: pip install lupa).

    python3 qsys/test/run_mock.py qsys/test/scenario_basic.lua

Runs from the repo root so dofile() paths inside scenarios resolve.
"""
import os
import sys
import tempfile

try:
    import lupa.lua53 as lupa
except ImportError:  # pragma: no cover
    sys.exit("lupa is required: pip install lupa")

if len(sys.argv) < 2:
    sys.exit(__doc__)

scenario = os.path.abspath(sys.argv[1])
repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# radio.lua writes media/_radio/settings.json and outbox.json relative to cwd; sandbox that in a temp dir
work = tempfile.mkdtemp(prefix="mockcore-")
os.symlink(os.path.join(repo, "qsys"), os.path.join(work, "qsys"))
os.chdir(work)

L = lupa.LuaRuntime(unpack_returned_tuples=True)
L.execute(open(scenario, encoding="utf-8").read())
