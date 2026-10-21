#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""For test/e2e-server.sh: runs diun-update.py as the helper runs (entry,
with its signal handling) with one difference: after the n-th rename of an
entry whose path contains <match> it writes "r" to the FIFO <ready> and waits
until it can read a byte from the FIFO <go>. The test sends its signal in
between: a synchronisation point on real data, no timing.

    e2e-pause.py <lib> <n> <match> <ready> <go> <helper arguments …>

Before the helper goes on it prints whether the signal waited
("@@SIGNAL-WHILE-HELD True": it came while the helper held signals back)."""
import importlib.util
import os
import sys

lib, n, match, ready, go = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
sys.path.insert(0, lib)
spec = importlib.util.spec_from_file_location("diun_update", os.path.join(lib, "diun-update.py"))
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)
import diunproc  # noqa: E402  (the helper's module of the signals)

real_rename = os.rename
count = [0]


def rename(a, b):
    real_rename(a, b)
    if match not in a:
        return
    count[0] += 1
    if count[0] == n:
        fd = os.open(ready, os.O_WRONLY)
        os.write(fd, b"r")
        os.close(fd)
        fd = os.open(go, os.O_RDONLY)     # SIGKILL ends the helper here
        os.read(fd, 1)
        os.close(fd)
        g = diunproc._guard
        print(f"@@SIGNAL-WHILE-HELD {g is not None and (g.pending is not None or g.got is not None)}", flush=True)


os.rename = rename
sys.exit(U.entry(sys.argv[6:]))
