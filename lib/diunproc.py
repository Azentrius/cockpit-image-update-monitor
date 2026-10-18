# SPDX-License-Identifier: Apache-2.0
"""Child processes with a time limit, for the helpers of Image Update Monitor.

Lives in /usr/lib/cockpit-image-updates next to the scripts that import it
(root-owned). Standard library only, no side effects on import.

Every command runs in a process group of its own. The limit is a deadline
for the whole command, checked while it prints and while it prints nothing.
When it passes, the group gets SIGTERM, after `grace` seconds SIGKILL, and
the process is always waited for: nothing is left behind as a zombie or a
reader of a dead pipe.

Killing a docker client does not undo what it already asked the Docker
daemon to do: a pull or a `compose up` may go on or stop half way. Callers
must say so instead of claiming a clean stop (Timeout.seconds, Timeout.tail).
"""

import os
import selectors
import signal
import subprocess
import time
from collections import deque

GRACE = 10          # seconds between SIGTERM and SIGKILL


class Timeout(Exception):
    """The command did not end within its limit; it has been stopped.
    tail: its last lines (stream) for the message."""
    def __init__(self, cmd, seconds, tail=()):
        super().__init__(f"{' '.join(cmd[:4])} … did not end within {seconds} s")
        self.cmd, self.seconds, self.tail = list(cmd), seconds, list(tail)


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
    always waited for."""
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


def stream(cmd, limit, out, keep=50, grace=GRACE):
    """Run cmd, pass its output (stdout and stderr) through to `out` line by
    line as it comes. Returns (exit code, its last `keep` lines). limit:
    seconds for the whole command (None: no limit). Raises Timeout after
    stopping it."""
    tail = deque(maxlen=keep)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    deadline = None if limit is None else time.monotonic() + limit
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    rest = b""
    try:
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
        sel.close()
        proc.stdout.close()


def copy(cmd, sink, limit, errfile, grace=GRACE):
    """Run cmd and hand its stdout to sink(bytes) chunk by chunk; stderr goes
    to errfile (a binary file). Returns the exit code. limit as in stream;
    raises Timeout after stopping it. A database dump: a process that writes
    nothing for hours counts as hanging only once the limit has passed, so
    the limit must leave room for the largest database."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfile, start_new_session=True)
    deadline = None if limit is None else time.monotonic() + limit
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    try:
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
        sel.close()
        proc.stdout.close()
