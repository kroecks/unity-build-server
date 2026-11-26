#!/usr/bin/env python3
"""
Detached build runner — the piece that lets a build outlive the server.

The server used to own build.sh's stdout pipe, so stopping or restarting it
(launchd kickstart, ./dev/restart.sh, an upgrade) killed the build: asyncio
kills child processes when its loop shuts down, and anything still writing to
the broken pipe gets SIGPIPE. Now the server starts THIS script in its own
session and goes back to watching files. The runner:

  * runs the build command with its output on a pipe it owns,
  * appends that output to the build log,
  * timestamps each ">>> phase" header into <id>.phases.jsonl (the log has no
    timestamps, and the server may be down when a phase starts),
  * writes <id>.exit.json {"rc", "finished_at"} when the command exits.

The server (or a restarted server) follows the log, phases and exit files, so
it never needs to be this process's parent. Only the standard library is used,
and the server runs it with `python -I`, so the venv and cwd don't matter.

usage: runner.py <build_id> <log> <phases.jsonl> <exit.json> -- <cmd> [args...]
"""

import json
import os
import re
import signal
import subprocess
import sys
import time

_ANSI_RE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")


def _phase_header(line: bytes) -> str:
    ln = _ANSI_RE.sub(b"", line).strip()
    if not ln.startswith(b">>> "):
        return ""
    return ln[4:].strip().decode(errors="replace")[:160]


def _write_json_atomic(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    if "--" not in argv or argv.index("--") != 4:
        print(__doc__, file=sys.stderr)
        return 2
    _build_id, log_path, phases_path, exit_path = argv[:4]
    cmd = argv[5:]

    # Never die with the terminal / server; a cancel signals the whole process
    # group, so the build gets SIGTERM directly and we just wait for it to exit
    # and record how it went.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, lambda *_: None)

    try:
        child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
    except Exception as e:
        with open(log_path, "ab") as lf:
            lf.write(f"\n=== runner could not start the build: {e!r} ===\n".encode())
        _write_json_atomic(exit_path, {"rc": 127, "finished_at": time.time()})
        return 1

    pending = b""
    log = open(log_path, "ab", buffering=0)
    phases = open(phases_path, "a", buffering=1)
    log_ok = True
    fd = child.stdout.fileno()
    while True:
        try:
            chunk = os.read(fd, 65536)
        except InterruptedError:
            continue
        if not chunk:
            break
        if log_ok:
            try:
                log.write(chunk)
            except OSError:
                # Disk full etc.: keep draining so the build never blocks on a
                # full pipe; it'll probably fail by itself shortly anyway.
                log_ok = False
        pending += chunk
        *lines, pending = pending.split(b"\n")
        if len(pending) > 65536:
            pending = b""   # giant unterminated line (IL2CPP); not a header
        for ln in lines:
            name = _phase_header(ln)
            if name:
                try:
                    phases.write(json.dumps({"name": name, "t": time.time()}) + "\n")
                except OSError:
                    pass
    rc = child.wait()
    try:
        log.close(); phases.close()
    except OSError:
        pass
    _write_json_atomic(exit_path, {"rc": rc, "finished_at": time.time()})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
