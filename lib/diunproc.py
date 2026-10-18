# SPDX-License-Identifier: Apache-2.0
"""Child processes with a time limit, for the helpers of Image Update Monitor.

Lives in /usr/lib/cockpit-image-updates next to the scripts that import it
(root-owned). Standard library only, no side effects on import: no signal
handler changes until a caller enters on_signals().

Every command runs in a process group of its own. The limit is a deadline
for the whole command, checked while it prints and while it prints nothing.
When it passes, the group gets SIGTERM, after `grace` seconds SIGKILL, and
the process is always waited for: nothing is left behind as a zombie or a
reader of a dead pipe.

Signals. Inside on_signals(), the first SIGTERM, SIGHUP or SIGINT raises
Interrupted in the main thread, so the caller's cleanup runs as for any
other error: the running command's group is stopped as above, the caller
takes back or records what it did, and only then does the process end (and
with it its lock). Signals that come during that cleanup are counted, not
raised: a second SIGTERM cannot cut the cleanup short. held() defers them
over a few steps that must not be cut in two (starting a process, writing a
history line); one that came meanwhile is raised at the end.

SIGKILL cannot be caught: the helper ends at once, without any cleanup. Its
commands run on in their own session. Started with keep_fds (the caller's
lock file), they keep the lock held until they end, so no other update or
rollback starts while they still change something.

Killing a docker client does not undo what it already asked the Docker
daemon to do: a pull or a `compose up` may go on or stop half way, and a
process started with `docker exec` runs on in the container until it notices
that its output is gone. Callers must say so instead of claiming a clean
stop (Timeout.seconds, Timeout.tail).
"""

import os
import selectors
import signal
import subprocess
import time
from collections import deque
from contextlib import contextmanager

GRACE = 10          # seconds between SIGTERM and SIGKILL
SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


class Timeout(Exception):
    """The command did not end within its limit; it has been stopped.
    tail: its last lines (stream) for the message."""
    def __init__(self, cmd, seconds, tail=()):
        super().__init__(f"{' '.join(cmd[:4])} … did not end within {seconds} s")
        self.cmd, self.seconds, self.tail = list(cmd), seconds, list(tail)


class Interrupted(BaseException):
    """The helper got SIGTERM, SIGHUP or SIGINT inside on_signals(). Like
    KeyboardInterrupt not an Exception: `except Exception` does not stop it
    on its way to the code that cleans up."""
    def __init__(self, signum):
        self.signum = signum
        self.name = signal.Signals(signum).name
        super().__init__(self.name)


class _Guard:
    """What on_signals() knows: the signal that started the stop (got), the
    ones that came after it (repeats), held() sections open (depth) and a
    signal that came inside one (pending)."""
    def __init__(self):
        self.got, self.repeats, self.depth, self.pending = None, 0, 0, None

    def handle(self, signum, frame):
        # No output here: a handler that writes can meet the main code in
        # the middle of a write to the same stream.
        if self.got is not None:
            self.repeats += 1
        elif self.depth:
            self.pending = signum
        else:
            self.got = signum
            raise Interrupted(signum)


_guard = None


@contextmanager
def on_signals(signums=SIGNALS):
    """Turn the first of these signals into Interrupted (see the module
    text); yields the guard (got, repeats). The handlers that were there
    before come back on exit. Main thread only."""
    global _guard
    if _guard is not None:
        raise RuntimeError("on_signals() is active already")
    guard, old = _Guard(), {}
    try:
        for s in signums:
            old[s] = signal.signal(s, guard.handle)
        _guard = guard
        yield guard
    finally:
        _guard = None
        for s, h in old.items():
            signal.signal(s, signal.SIG_DFL if h is None else h)


def _hold():
    if _guard is not None:
        _guard.depth += 1


def _release():
    """End a held section; raise the signal that came inside it, if any and
    no other one has started the stop yet."""
    g = _guard
    if g is None:
        return
    g.depth -= 1
    if not g.depth and g.pending is not None:
        signum, g.pending = g.pending, None
        if g.got is None:
            g.got = signum
            raise Interrupted(signum)
        g.repeats += 1


@contextmanager
def held():
    """Defer the signals of on_signals() until the block is done (nothing
    outside on_signals())."""
    _hold()
    try:
        yield
    finally:
        _release()


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop(proc, grace=GRACE):
    """End the whole process group of proc (also what it started and left
    behind): SIGTERM, up to `grace` seconds to end, then SIGKILL. proc is
    always waited for; a signal meanwhile is raised after that (held)."""
    with held():
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            proc.wait()
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and (proc.poll() is None or _group_alive(proc.pid)):
            time.sleep(0.05)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


def _left(deadline):
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def _start(cmd, keep_fds, **kw):
    """Popen in a session of its own, with the signals of on_signals() held
    from before the start until the caller's try, which stops the process on
    any exception: a signal can never leave a started command behind. The
    caller calls _release() first thing inside that try."""
    _hold()
    try:
        return subprocess.Popen(cmd, start_new_session=True, pass_fds=tuple(keep_fds), **kw)
    except BaseException:
        _release()
        raise


def stream(cmd, limit, out, keep=50, grace=GRACE, keep_fds=()):
    """Run cmd, pass its output (stdout and stderr) through to `out` line by
    line as it comes. Returns (exit code, its last `keep` lines). limit:
    seconds for the whole command (None: no limit). Raises Timeout after
    stopping it. keep_fds: open files the command inherits (a lock it keeps
    held while it runs, see the module text)."""
    tail = deque(maxlen=keep)
    proc = _start(cmd, keep_fds, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    sel = None
    try:
        _release()
        deadline = None if limit is None else time.monotonic() + limit
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        rest = b""
        while True:
            left = _left(deadline)
            if left == 0.0:
                raise Timeout(cmd, limit, tail)
            if not sel.select(left):
                continue
            data = os.read(proc.stdout.fileno(), 1 << 16)
            if not data:
                break
            rest += data
            *lines, rest = rest.split(b"\n")
            for line in lines:
                text = line.decode("utf-8", "replace")
                out.write(text + "\n")
                tail.append(text.rstrip("\r"))
            out.flush()
        if rest:
            text = rest.decode("utf-8", "replace")
            out.write(text + "\n")
            out.flush()
            tail.append(text.rstrip("\r"))
        left = _left(deadline)
        try:
            rc = proc.wait(timeout=left)
        except subprocess.TimeoutExpired:
            raise Timeout(cmd, limit, tail)
        return rc, list(tail)
    except BaseException:
        stop(proc, grace)
        raise
    finally:
        if sel is not None:
            sel.close()
        proc.stdout.close()


def copy(cmd, sink, limit, errfile, grace=GRACE, keep_fds=()):
    """Run cmd and hand its stdout to sink(bytes) chunk by chunk; stderr goes
    to errfile (a binary file). Returns the exit code. limit and keep_fds as
    in stream; raises Timeout after stopping it. A database dump: a process
    that writes nothing for hours counts as hanging only once the limit has
    passed, so the limit must leave room for the largest database."""
    proc = _start(cmd, keep_fds, stdout=subprocess.PIPE, stderr=errfile)
    sel = None
    try:
        _release()
        deadline = None if limit is None else time.monotonic() + limit
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        while True:
            left = _left(deadline)
            if left == 0.0:
                raise Timeout(cmd, limit)
            if not sel.select(left):
                continue
            data = os.read(proc.stdout.fileno(), 1 << 16)
            if not data:
                break
            sink(data)
        try:
            return proc.wait(timeout=_left(deadline))
        except subprocess.TimeoutExpired:
            raise Timeout(cmd, limit)
    except BaseException:
        stop(proc, grace)
        raise
    finally:
        if sel is not None:
            sel.close()
        proc.stdout.close()
