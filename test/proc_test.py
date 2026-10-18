#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for lib/diunproc.py with real child processes (Linux): a
command that hangs without a word, one that prints without end, one whose
children ignore SIGTERM, a dump that hangs; each must end at its limit, with
its whole process group gone, and the next command must run as usual.
Limits of a fraction of a second keep the run short; what is checked does
not depend on timing (a hang never ends by itself).

Signals: a helper process (a small driver around stream / copy that holds a
lock as diun-update.py does) gets SIGTERM or SIGHUP while its command runs,
silent or printing. The commands report through a FIFO when they start and
when the stop reaches them, and then wait for the test: fixed points at which
the lock must still be held. Also children that ignore SIGTERM, signals again
during the cleanup, and SIGKILL, which no cleanup can follow."""
import errno
import fcntl
import importlib.util
import io
import os
import select
import signal
import subprocess
import sys
import tempfile
import time

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
HANDLERS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
BEFORE_IMPORT = {s: signal.getsignal(s) for s in HANDLERS}
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

# ------------------------------------------------------------------- signals
check("import: no signal handler changed", {s: signal.getsignal(s) for s in HANDLERS}, BEFORE_IMPORT)


def mine(signum, frame):
    pass


signal.signal(signal.SIGHUP, mine)
with P.on_signals() as g:
    inside = signal.getsignal(signal.SIGHUP)
    try:
        with P.on_signals():
            nested = False
    except RuntimeError:
        nested = True
    raised, after_kill = None, None
    try:
        with P.held():
            os.kill(os.getpid(), signal.SIGHUP)
            after_kill = "went on"
    except P.Interrupted as e:
        raised = e.name
    os.kill(os.getpid(), signal.SIGTERM)       # after the first: counted, not raised
    time.sleep(0.05)
check("on_signals: own handler inside, no second one, held() defers to its end, repeats counted",
      (inside is not mine, nested, after_kill, raised, g.got, g.repeats),
      (True, True, "went on", "SIGHUP", signal.SIGHUP, 1))
check("on_signals: the handlers from before are back",
      (signal.getsignal(signal.SIGHUP), signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)),
      (mine, BEFORE_IMPORT[signal.SIGTERM], BEFORE_IMPORT[signal.SIGINT]))
signal.signal(signal.SIGHUP, BEFORE_IMPORT[signal.SIGHUP])

TMP = tempfile.mkdtemp(prefix="diunproc-test-")
LOCK = os.path.join(TMP, "update.lock")
EV, GO = os.path.join(TMP, "events"), os.path.join(TMP, "go")
os.mkfifo(EV)
os.mkfifo(GO)
ev_r = os.open(EV, os.O_RDONLY | os.O_NONBLOCK)
ev_keep = os.open(EV, os.O_WRONLY)          # no end of file between two writers
DRIVER = os.path.join(TMP, "driver.py")
with open(DRIVER, "w") as f:
    f.write('''
import fcntl, importlib.util, os, sys, tempfile
lib, lock, how, grace, keep = sys.argv[1:6]
cmd = sys.argv[6:]
spec = importlib.util.spec_from_file_location("diunproc", os.path.join(lib, "diunproc.py"))
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)
fh = open(lock, "w")
fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
fds = (fh.fileno(),) if keep == "1" else ()
guard = None
try:
    with P.on_signals() as guard:
        if how == "stream":
            rc, _ = P.stream(cmd, None, sys.stdout, grace=float(grace), keep_fds=fds)
        else:
            with tempfile.TemporaryFile() as err:
                rc = P.copy(cmd, lambda data: None, None, err, grace=float(grace), keep_fds=fds)
    print("ended", rc, flush=True)
    sys.exit(0)
except P.Interrupted as e:
    print("interrupted", e.name, "repeats", guard.repeats, flush=True)
    sys.exit(128 + e.signum)
''')
pending = b""


def event(word, within=60):
    """The next line the commands wrote to the FIFO: (word, [numbers])."""
    global pending
    end = time.monotonic() + within
    while b"\n" not in pending:
        left = end - time.monotonic()
        if left <= 0 or not select.select([ev_r], [], [], left)[0]:
            return None, []
        pending += os.read(ev_r, 4096)
    line, pending = pending.split(b"\n", 1)
    parts = line.decode().split()
    return (parts[0], [int(x) for x in parts[1:]]) if parts and parts[0] == word else (line.decode(), [])


def go(within=30):
    """Let a command that waits in its TERM trap end."""
    end = time.monotonic() + within
    while True:
        try:
            fd = os.open(GO, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError as e:
            if e.errno != errno.ENXIO or time.monotonic() > end:
                raise
            time.sleep(0.02)
    os.write(fd, b"go\n")
    os.close(fd)


def lock_free():
    with open(LOCK, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh, fcntl.LOCK_UN)
        return True


def driver(how, script, grace=30, keep=False):
    out = tempfile.TemporaryFile()
    proc = subprocess.Popen([sys.executable, DRIVER, ROOT, LOCK, how, str(grace), "1" if keep else "0",
                             "sh", "-c", script], stdout=out, stderr=subprocess.STDOUT,
                            env={**os.environ, "EV": EV, "GO": GO})
    return proc, out


def output(out):
    out.seek(0)
    return out.read().decode()


# A command that ends on SIGTERM once the test lets it (it reports the
# signal first): silent, or printing all the time.
ENDS = 'trap \'echo term > "$EV"; read x < "$GO"; exit 0\' TERM\necho "start $$" > "$EV"\n'
SILENT = ENDS + "while :; do sleep 0.2; done"
PRINTS = ENDS + "while :; do echo tick; sleep 0.02; done"
for sig, how, script, kind in ((signal.SIGTERM, "stream", SILENT, "silent"),
                               (signal.SIGTERM, "copy", PRINTS, "printing"),
                               (signal.SIGHUP, "copy", SILENT, "silent"),
                               (signal.SIGHUP, "stream", PRINTS, "printing")):
    name = f"{signal.Signals(sig).name}, {kind} command ({how})"
    proc, out = driver(how, script)
    started, pids = event("start")
    proc.send_signal(sig)
    reached, _ = event("term")
    # The command got the stop and waits: the helper is in its cleanup.
    during = (proc.poll() is None, lock_free())
    go()
    rc = proc.wait(timeout=60)
    check(f"{name}: the stop reached the command while the helper still held the lock",
          (started, reached, during), ("start", "term", (True, False)))
    check(f"{name}: the helper ends with 128 + signal after the cleanup, the command is gone, the lock free",
          (rc, output(out).splitlines()[-1], bool(pids) and gone_soon(pids[0]), lock_free()),
          (128 + sig, f"interrupted {signal.Signals(sig).name} repeats 0", True, True))

# Children that ignore SIGTERM (the command handles it without ending, its
# child ignores it), and SIGTERM and SIGHUP again during the cleanup: the
# cleanup goes on to SIGKILL after the grace time; nothing is cut short.
IGNORES = ('trap \'echo term > "$EV"\' TERM\n'
           'sh -c \'trap "" TERM; echo "start $PPID $$" > "$EV"; exec sleep 600\' &\n'
           'while :; do sleep 0.2; done')
proc, out = driver("stream", IGNORES, grace=4)
started, pids = event("start")
t0 = time.monotonic()
proc.send_signal(signal.SIGTERM)
reached, _ = event("term")
proc.send_signal(signal.SIGTERM)
time.sleep(0.2)
proc.send_signal(signal.SIGHUP)
time.sleep(0.2)
during = (proc.poll() is None, lock_free(), all(alive(p) for p in pids))
rc = proc.wait(timeout=60)
took = time.monotonic() - t0
check("ignoring SIGTERM, signals again: the helper keeps the lock, its command and the child keep running",
      (started, len(pids), reached, during), ("start", 2, "term", (True, False, True)))
check("ignoring SIGTERM, signals again: SIGKILL after the grace time, both gone, the repeats only counted",
      (rc, output(out).splitlines()[-1], took >= 3.5, [gone_soon(p) for p in pids], lock_free()),
      (128 + signal.SIGTERM, "interrupted SIGTERM repeats 2", True, [True, True], True))

# The next operation runs as usual.
proc, out = driver("stream", 'echo "start $$" > "$EV"; echo done')
event("start")
check("after the signals: the next operation takes the lock and runs", (proc.wait(timeout=60), output(out)),
      (0, "done\nended 0\n"))

# SIGKILL: no cleanup can run. The command runs on in its own session; with
# the lock passed on (keep_fds) the lock stays held until it ends as well.
proc, out = driver("stream", 'echo "start $$" > "$EV"; while :; do sleep 0.2; done', keep=True)
started, pids = event("start")
proc.send_signal(signal.SIGKILL)
rc = proc.wait(timeout=60)
after_kill = (rc, bool(pids) and alive(pids[0]), lock_free())
if pids:
    os.killpg(pids[0], signal.SIGKILL)
check("SIGKILL: the helper ends at once, its command runs on and keeps the lock",
      (started, after_kill), ("start", (-signal.SIGKILL, True, False)))
check("SIGKILL: once the command has ended, the lock is free", (bool(pids) and gone_soon(pids[0]), lock_free()),
      (True, True))

os.close(ev_r)
os.close(ev_keep)
for p in (EV, GO, DRIVER, LOCK):
    if os.path.exists(p):
        os.unlink(p)
os.rmdir(TMP)

print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
