"""
Host utilisation for the UI's stats widget: CPU, memory, disk space, disk I/O,
and what the running build itself is using — enough to tell at a glance whether
the mini is saturated or has room for more work.

A background sampler takes a reading every SAMPLE_S seconds and keeps the last
~10 minutes, so the widget can draw sparklines and the numbers are averaged
over a real interval (a one-shot CPU reading is meaningless).

Uses psutil when installed (requirements.txt). Without it, falls back to
stdlib + macOS command-line tools (sysctl / vm_stat) or /proc on Linux, with
fewer details (no I/O rates, no per-build process stats).
"""

import asyncio
import os
import re
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    psutil = None

SAMPLE_S = 3.0
HISTORY_N = 200          # 200 × 3 s = 10 min of sparkline
SAMPLES: "deque[dict]" = deque(maxlen=HISTORY_N)
VOLUMES: list[Path] = []  # set by the server: output_root, builds_root, log_dir
_last_io = None           # (time, read_bytes, write_bytes)
_proc_cache: dict = {}    # pid -> psutil.Process (cpu_percent needs the same object)
_fallback_cpu = None      # (time, busy_ticks, total_ticks) for /proc fallback


def cores() -> int:
    if psutil:
        return psutil.cpu_count(logical=True) or 1
    return os.cpu_count() or 1


# --------------------------------------------------------------------------- #
# Fallbacks (no psutil)
# --------------------------------------------------------------------------- #

def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""


def _fallback_cpu_pct() -> Optional[float]:
    """Linux: /proc/stat deltas. macOS: load average / cores as a proxy."""
    global _fallback_cpu
    try:
        with open("/proc/stat") as f:
            vals = [int(x) for x in f.readline().split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        total = sum(vals)
        prev, _fallback_cpu = _fallback_cpu, (time.time(), total - idle, total)
        if prev and total > prev[2]:
            return round(100.0 * ((total - idle) - prev[1]) / (total - prev[2]), 1)
        return None
    except OSError:
        pass
    try:
        return round(min(100.0, os.getloadavg()[0] / cores() * 100), 1)
    except OSError:
        return None


def _fallback_mem() -> dict:
    try:   # Linux
        info = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                k, v = ln.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        total, avail = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
        return {"total": total, "used": total - avail, "available": avail,
                "swap_total": info.get("SwapTotal", 0),
                "swap_used": info.get("SwapTotal", 0) - info.get("SwapFree", 0)}
    except (OSError, KeyError, ValueError):
        pass
    total = int((_run(["sysctl", "-n", "hw.memsize"]).strip() or "0"))
    vm = _run(["vm_stat"])
    page = int((re.search(r"page size of (\d+)", vm) or [None, "4096"])[1])
    pages = {m[0]: int(m[1]) for m in re.findall(r'^"?([^:"]+)"?:\s+(\d+)\.', vm, re.M)}
    # Same notion of "available" as Activity Monitor's "memory used" complement.
    avail = (pages.get("Pages free", 0) + pages.get("Pages inactive", 0)
             + pages.get("Pages speculative", 0) + pages.get("Pages purgeable", 0)) * page
    return {"total": total, "used": max(0, total - avail), "available": avail,
            "swap_total": 0, "swap_used": 0}


# --------------------------------------------------------------------------- #
# Sampling
# --------------------------------------------------------------------------- #

def _volumes() -> list[dict]:
    seen, out = set(), []
    for p in VOLUMES:
        try:
            p = Path(p).expanduser()
            while not p.exists() and p != p.parent:
                p = p.parent
            st = os.statvfs(str(p))
            dev = os.stat(str(p)).st_dev
        except OSError:
            continue
        if dev in seen:
            continue
        seen.add(dev)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        out.append({"path": str(p), "total": total, "free": free, "used": total - free,
                    "pct": round((total - free) / total * 100, 1) if total else None})
    return out


def _build_procs(root_pid: int) -> Optional[dict]:
    """CPU % (100 = one core) and RSS of the build's whole process tree."""
    if not psutil or root_pid <= 0:
        return None
    try:
        root = psutil.Process(root_pid)
        procs = [root] + root.children(recursive=True)
    except psutil.Error:
        return None
    cpu = rss = 0.0
    alive = set()
    top = []
    for p in procs:
        cached = _proc_cache.get(p.pid)
        if cached is None or cached.create_time() != p.create_time():
            _proc_cache[p.pid] = cached = p
            try:
                cached.cpu_percent(None)       # prime; first reading is 0
            except psutil.Error:
                continue
        alive.add(p.pid)
        try:
            c = cached.cpu_percent(None)
            m = cached.memory_info().rss
            cpu += c; rss += m
            top.append((c, m, cached.name()))
        except psutil.Error:
            continue
    for pid in list(_proc_cache):
        if pid not in alive:
            _proc_cache.pop(pid, None)
    top.sort(reverse=True)
    return {"cpu": round(cpu, 1), "rss": int(rss), "count": len(alive),
            "top": [{"name": n, "cpu": round(c, 1), "rss": int(m)} for c, m, n in top[:3]]}


def sample(build_pid: int = 0) -> dict:
    global _last_io
    now = time.time()
    s: dict = {"t": now, "cores": cores()}
    try:
        s["load"] = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        s["load"] = None
    if psutil:
        s["cpu"] = round(psutil.cpu_percent(None), 1)
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        s["mem"] = {"total": vm.total, "used": vm.total - vm.available,
                    "available": vm.available, "swap_total": sw.total, "swap_used": sw.used}
        try:
            io = psutil.disk_io_counters()
        except Exception:
            io = None
        if io is not None:
            if _last_io and now > _last_io[0]:
                dt = now - _last_io[0]
                s["io"] = {"read_bps": max(0, (io.read_bytes - _last_io[1]) / dt),
                           "write_bps": max(0, (io.write_bytes - _last_io[2]) / dt)}
            _last_io = (now, io.read_bytes, io.write_bytes)
    else:
        s["cpu"] = _fallback_cpu_pct()
        s["mem"] = _fallback_mem()
    m = s["mem"]
    m["pct"] = round(m["used"] / m["total"] * 100, 1) if m.get("total") else None
    s["disks"] = _volumes()
    s["build"] = _build_procs(build_pid) if build_pid else None
    return s


async def sampler(get_build_pid: Callable[[], int]) -> None:
    """Background task: one sample every SAMPLE_S seconds, forever."""
    if psutil:
        psutil.cpu_percent(None)   # prime the CPU counter
    while True:
        try:
            SAMPLES.append(await asyncio.to_thread(sample, get_build_pid() or 0))
        except Exception as e:     # never let stats take the server down
            print(f"[ubs] host stats sample failed: {e!r}", flush=True)
        await asyncio.sleep(SAMPLE_S)


def snapshot() -> dict:
    """Latest reading plus compact series for sparklines."""
    hist = list(SAMPLES)
    cur = hist[-1] if hist else sample()

    def disk_pct(x):   # the fullest watched volume is the one that matters
        pcts = [d["pct"] for d in x.get("disks") or [] if d.get("pct") is not None]
        return max(pcts) if pcts else None
    return {
        "now": cur,
        "psutil": psutil is not None,
        "interval_s": SAMPLE_S,
        "series": {
            "t": [round(x["t"]) for x in hist],
            "cpu": [x.get("cpu") for x in hist],
            "mem": [x["mem"].get("pct") for x in hist],
            "disk": [disk_pct(x) for x in hist],
            "build_cpu": [(x["build"] or {}).get("cpu") if x.get("build") else None for x in hist],
        },
    }
