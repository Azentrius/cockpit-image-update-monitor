#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for lib/diunproc.py with real child processes (Linux): a
command that hangs without a word, one that prints without end, one whose
children ignore SIGTERM, a dump that hangs; each must end at its limit, with
its whole process group gone, and the next command must run as usual.
Limits of a fraction of a second keep the run short; what is checked does
not depend on timing (a hang never ends by itself)."""
import importlib.util
import io
import os
import sys
import tempfile
import time

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
spec = importlib.util.spec_from_file_location("diunproc", os.path.join(ROOT, "diunproc.py"))
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("OK   " if ok else "FAIL ") + label + ("" if ok else f"\n     got  {got!r}\n     want {want!r}"))


def alive(pid):
    """A process that still runs (a zombie waiting for its parent does not)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False


def gone_soon(pid, within=5.0):
    """Orphans are reaped by init a moment after they die."""
    end = time.monotonic() + within
    while alive(pid) and time.monotonic() < end:
        time.sleep(0.02)
    return not alive(pid)


def timed_out(fn):
    try:
        return False, fn()
    except P.Timeout as e:
        return True, e


# A command that hangs and says nothing: stopped at its limit.
out = io.StringIO()
t0 = time.monotonic()
hit, e = timed_out(lambda: P.stream(["sh", "-c", "sleep 60"], 0.5, out, grace=1))
check("silent hang: Timeout at the limit, not at the end of the command",
      (hit, e.seconds if hit else None, time.monotonic() - t0 < 10), (True, 0.5, True))

# A command that never stops printing: the limit is for the whole command.
out = io.StringIO()
hit, e = timed_out(lambda: P.stream(["sh", "-c", "while :; do echo tick; sleep 0.01; done"], 0.5, out, grace=1))
check("endless output: Timeout all the same, the output passed through, the last lines kept",
      (hit, out.getvalue().startswith("tick\n"), e.tail[-1] if hit else None, len(e.tail) <= 50 if hit else None),
      (True, True, "tick", True))

# Children that ignore SIGTERM, one in the background holding the output open:
# SIGKILL for the whole group after the grace time.
out = io.StringIO()
hit, e = timed_out(lambda: P.stream(["sh", "-c", "trap '' TERM; sleep 60 & echo child $!; wait"], 0.5, out, grace=0.5))
child = int(out.getvalue().split()[1]) if out.getvalue().startswith("child ") else None
check("children that ignore SIGTERM: Timeout, then the whole group killed",
      (hit, child is not None and gone_soon(child)), (True, True))

# The next command after a timeout runs as usual: exit code, output, last lines.
out = io.StringIO()
rc, tail = P.stream(["sh", "-c", "echo one; echo two >&2; exit 3"], 10, out)
check("after a timeout: the next command as usual (exit code, both streams, last lines)",
      (rc, sorted(out.getvalue().splitlines()), sorted(tail)), (3, ["one", "two"], ["one", "two"]))
rc, tail = P.stream(["sh", "-c", "printf 'no newline at the end'"], None, out)
check("no limit, last line without a newline", (rc, tail[-1]), (0, "no newline at the end"))

# A dump: data to the sink, stderr to its file; a dump that hangs is stopped.
got = []
with tempfile.TemporaryFile() as err:
    rc = P.copy(["sh", "-c", "printf 'part1'; printf 'part2'; echo oops >&2; exit 0"], got.append, 10, err)
    err.seek(0)
    check("copy: data in order, stderr kept apart, exit code", (rc, b"".join(got), err.read()), (0, b"part1part2", b"oops\n"))
got = []
with tempfile.TemporaryFile() as err:
    hit, e = timed_out(lambda: P.copy(["sh", "-c", "printf 'start'; sleep 60"], got.append, 0.5, err, grace=1))
check("copy, a dump that hangs after its first bytes: Timeout, what came so far handed over",
      (hit, b"".join(got)), (True, b"start"))

# stop() on a process that already ended: nothing to do, no error.
proc = P.subprocess.Popen(["true"], start_new_session=True)
proc.wait()
P.stop(proc, grace=0.1)
check("stop() after the end: no error", proc.returncode, 0)

print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
