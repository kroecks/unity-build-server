#!/usr/bin/env python3
"""
Unity build server — multi-project edition.

The service owns a project registry and all per-project config. You onboard
projects through the web UI (clone from Gitea, or browse the mini's filesystem
for an already-cloned project); the service detects the Unity version, installs
the build entry point, and stores everything. Builds are triggered per project,
from the phone or from a Gitea push webhook that routes to the right project.

Endpoints
  GET  /                      phone + config UI
  GET  /api/projects          list projects
  POST /api/projects/clone    onboard by cloning a repo URL (registers at once,
                              downloads in the background; resumes on restart)
  POST /api/projects/{id}/retry-onboard   resume a failed clone
  GET  /api/projects/{id}/onboard-log     clone progress log
  POST /api/projects/adopt    onboard a project already on disk (path)
  PATCH /api/projects/{id}    edit Apple/behaviour fields
  DELETE /api/projects/{id}   remove (optionally delete the clone)
  POST /api/projects/{id}/verify   re-run detection + reinstall build script
  GET  /api/browse?path=...   list directories under the configured root
  POST /build/{id}            enqueue a build (token-gated)
  POST /webhook/gitea         enqueue by matching repo URL (HMAC-verified)
  GET  /status                queue + running + history
  GET  /logs/{build_id}       SSE live log (resumes via Last-Event-ID)
  GET  /api/host              CPU / memory / disk of the build machine
  GET  /logs/{build_id}/raw   full log text

Security unchanged from before: bind to the LAN IP, token on /build, HMAC on
the webhook, no request input is ever passed through a shell string.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Header, Query
from fastapi.responses import (HTMLResponse, JSONResponse, StreamingResponse,
                               PlainTextResponse, FileResponse)

from .registry import Registry, Project, RegistryWriteError
from . import onboarding
from . import devices as devmod
from . import repostatus
from . import hoststats
from .ui import INDEX_HTML

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

CONFIG_PATH = Path(os.environ.get("UBS_CONFIG", Path(__file__).parent.parent / "config" / "config.json"))
with open(CONFIG_PATH) as f:
    CFG = json.load(f)

BUILD_SCRIPT = str(Path(CFG["build_script"]).expanduser())
LOG_DIR = Path(CFG["log_dir"]).expanduser()
LOG_DIR.mkdir(parents=True, exist_ok=True)
BUILDS_ROOT = Path(CFG["builds_root"]).expanduser()      # where clones live
BROWSE_ROOT = Path(CFG.get("browse_root", str(Path.home()))).expanduser()  # dir-picker sandbox
REGISTRY_PATH = Path(CFG["registry_path"]).expanduser()
# Per-build output dirs (Xcode project, archive, .ipa) live here — point this at
# the USB if you like. Each build gets <OUTPUT_ROOT>/<project-id>/<build-id>/.
OUTPUT_ROOT = Path(CFG.get("output_root", str(BUILDS_ROOT / "output"))).expanduser()
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
# Retention: keep at most this many build output dirs per project (0 = keep all).
KEEP_BUILDS_PER_PROJECT = int(CFG.get("keep_builds_per_project", 10))

BUILD_TOKEN = CFG.get("build_token", "")
GITEA_WEBHOOK_SECRET = CFG.get("gitea_webhook_secret", "").encode()

# App Store Connect API key for TestFlight uploads (shared across projects).
# MUST be a TEAM key (ASC → Users and Access → Integrations → Team Keys), not an
# Individual key — altool/xcodebuild reject individual keys with "-26000 Failure
# to authenticate". Point asc_key_path at the AuthKey_<KEYID>.p8 file.
ASC_KEY_ID = CFG.get("asc_key_id", "")
ASC_ISSUER_ID = CFG.get("asc_issuer_id", "")
ASC_KEY_PATH = str(Path(CFG["asc_key_path"]).expanduser()) if CFG.get("asc_key_path") else ""

# Android keystore PASSWORDS (host-level secrets, like the ASC key — never in the
# registry or the UI). Keyed by keystore path as entered on the project; a
# "default" entry applies to any keystore without its own entry:
#   "android_keystores": {
#     "~/keys/voxelmage.keystore": {"store_pass": "...", "key_pass": "..."},
#     "default":                   {"store_pass": "...", "key_pass": "..."}
#   }
# key_pass defaults to store_pass if omitted (common for single-key keystores).
ANDROID_KEYSTORES = CFG.get("android_keystores", {}) or {}

# adb: explicit config path, else PATH (brew android-platform-tools), else the
# SDK bundled with any Hub-installed editor's Android module.
ADB_CFG = str(Path(CFG["adb_path"]).expanduser()) if CFG.get("adb_path") else ""

PLATFORMS = {
    # platform -> (Unity -buildTarget, BuildScript entry point)
    "ios": ("iOS", "BuildScript.PerformiOSBuild"),
    "android": ("Android", "BuildScript.PerformAndroidBuild"),
}


def _keystore_passwords(keystore_path: str) -> tuple[str, str]:
    """(store_pass, key_pass) for a keystore from host config. Matches on the
    path as typed or expanded; falls back to the "default" entry."""
    if not keystore_path:
        return "", ""
    exp = str(Path(keystore_path).expanduser())
    ent = None
    for k, v in ANDROID_KEYSTORES.items():
        if k == "default":
            continue
        if k == keystore_path or str(Path(k).expanduser()) == exp:
            ent = v; break
    if ent is None:
        ent = ANDROID_KEYSTORES.get("default") or {}
    store = str(ent.get("store_pass", "") or "")
    key = str(ent.get("key_pass", "") or store)
    return store, key


def _adb_path() -> str:
    if ADB_CFG and Path(ADB_CFG).exists():
        return ADB_CFG
    w = shutil.which("adb", path="/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", ""))
    if w:
        return w
    for root in (Path.home() / "Applications/Unity/Hub/Editor", Path("/Applications/Unity/Hub/Editor")):
        hits = sorted(root.glob("*/PlaybackEngines/AndroidPlayer/SDK/platform-tools/adb"), reverse=True)
        if hits:
            return str(hits[0])
    return "adb"   # let it fail with a clear "command not found"

reg = Registry(REGISTRY_PATH)
# Device inventory (phones / tablets / headsets) — see app/devices.py.
DEVICES_PATH = Path(CFG.get("devices_path", str(REGISTRY_PATH.parent / "devices.json"))).expanduser()
DEVICES = devmod.DeviceStore(DEVICES_PATH, _adb_path)
app = FastAPI(title="Unity Build Server")


@app.exception_handler(RegistryWriteError)
async def _registry_write_error(request, exc):
    # Disk full / unwritable registry → a clean 507 with a readable message,
    # instead of a 500 stack trace. The UI surfaces exc's text to the user.
    return JSONResponse(status_code=507, content={"detail": str(exc)})


# --------------------------------------------------------------------------- #
# Build queue (one at a time, across all projects — never two Unity builds at once)
# --------------------------------------------------------------------------- #


@dataclass
class Build:
    id: str
    project_id: str
    project_name: str
    source: str
    deploy: bool
    state: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None    # when it began running
    finished_at: Optional[float] = None   # when it reached a terminal state
    returncode: Optional[int] = None
    output_dir: str = ""
    artifact: str = ""          # path to the final .ipa/.app/archive, if produced
    version: str = ""           # Unity bundleVersion, e.g. "1.4.2"
    build_number: str = ""      # the (auto-incremented) build number, e.g. "57"
    commit_sha: str = ""        # the git commit this build came from (short)
    label: str = ""             # user-assigned name/tag for this build
    platform: str = "ios"       # project platform at enqueue time (UI hides iOS-only actions)
    # Timing breakdown: one entry per ">>> " phase header build.sh prints, with
    # its start offset (seconds since started_at). Durations are derived.
    phases: list = field(default_factory=list)
    # Device ids picked for this run (empty = the project's default targets,
    # resolved when the build actually runs). deployed_to records what was
    # online and handed to the build script.
    devices: list = field(default_factory=list)
    deployed_to: list = field(default_factory=list)
    # pid of the detached runner (app/runner.py) executing this build. It's a
    # session leader, so it is also the process group to signal on cancel.
    runner_pid: int = 0

    def duration(self) -> Optional[float]:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    @property
    def log_path(self) -> Path:
        return LOG_DIR / f"{self.id}.log"

    def public(self) -> dict:
        d = asdict(self)
        d["log_url"] = f"/logs/{self.id}"
        d["created_iso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at))
        started = self.started_at if self.started_at is not None else self.created_at
        d["started_iso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
        d["elapsed"] = self.duration()                      # seconds, live if running
        d["has_artifact"] = bool(self.artifact and Path(self.artifact).exists())
        d["artifact_url"] = f"/artifact/{self.id}" if d["has_artifact"] else ""
        # Human-friendly version label, e.g. "1.4.2 (57)". Empty if unknown.
        if self.version and self.build_number:
            d["version_label"] = f"{self.version} ({self.build_number})"
        elif self.version:
            d["version_label"] = self.version
        elif self.build_number:
            d["version_label"] = f"build {self.build_number}"
        else:
            d["version_label"] = ""
        d["commit_short"] = (self.commit_sha or "")[:8]
        d.pop("phases", None)
        d.pop("runner_pid", None)
        d["timing"] = self.timing()
        d["device_names"] = [_device_name(x) for x in (self.deployed_to or self.devices)]
        return d

    def timing(self) -> list:
        """Phases with durations: each runs until the next starts (the last one
        until the build finished, or now if it's still running)."""
        if self.started_at is None or not self.phases:
            return []
        end = (self.finished_at if self.finished_at is not None else time.time()) - self.started_at
        out = []
        for i, ph in enumerate(self.phases):
            nxt = self.phases[i + 1]["start"] if i + 1 < len(self.phases) else end
            out.append({"name": ph["name"], "start": round(ph["start"], 1),
                        "duration": round(max(0.0, nxt - ph["start"]), 1)})
        return out


QUEUE: "deque[Build]" = deque()
HISTORY: "deque[Build]" = deque(maxlen=500)   # keep plenty; UI pages through them
CURRENT: Optional[Build] = None
_wakeup = asyncio.Event()
# A build that was running when the server last stopped and whose runner is
# still alive (or finished while we were down). The worker adopts it before
# starting anything new. Set by load_active() at startup.
_REATTACH: Optional[Build] = None

# Persist finished builds so history survives a server restart.
HISTORY_PATH = LOG_DIR / "history.json"
# The running build + the queue, so both survive a restart too.
ACTIVE_PATH = LOG_DIR / "active.json"
RUNNER = Path(__file__).with_name("runner.py")
_BUILD_FIELDS = set(Build.__dataclass_fields__)


def _build_from_rec(rec: dict) -> Build:
    return Build(**{k: v for k, v in rec.items() if k in _BUILD_FIELDS})


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def save_history() -> None:
    try:
        _write_json(HISTORY_PATH, [asdict(b) for b in list(HISTORY)])
    except Exception:
        pass  # history is a convenience; never let it break a build


def load_history() -> None:
    if not HISTORY_PATH.exists():
        return
    try:
        for rec in json.loads(HISTORY_PATH.read_text()):
            # Old servers could leave these in history; they didn't finish.
            if rec.get("state") in ("running", "queued"):
                rec["state"] = "interrupted"
            HISTORY.append(_build_from_rec(rec))
    except Exception:
        pass


def save_active() -> None:
    """Snapshot the running build and the queue. Called on every change, so a
    restart (or crash) at any point picks up exactly where things were."""
    try:
        _write_json(ACTIVE_PATH, {"current": asdict(CURRENT) if CURRENT else None,
                                  "queue": [asdict(b) for b in QUEUE]})
    except Exception as e:
        print(f"[ubs] could not save {ACTIVE_PATH}: {e!r}", flush=True)


def _phases_path(b: Build) -> Path:
    return LOG_DIR / f"{b.id}.phases.jsonl"


def _exit_path(b: Build) -> Path:
    return LOG_DIR / f"{b.id}.exit.json"


def _read_exit(b: Build) -> Optional[dict]:
    try:
        return json.loads(_exit_path(b).read_text())
    except (OSError, ValueError):
        return None


def _runner_alive(pid: int, build_id: str, verify_cmdline: bool = False) -> bool:
    """Is `pid` still the runner for this build? verify_cmdline guards against
    pid reuse after a reboot or a long outage (checked once, on reattach)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False          # someone else's process now
    if not verify_cmdline:
        return True
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return True           # can't tell; assume it's ours rather than orphan it
    return "runner.py" in out and build_id in out


def load_active() -> None:
    """Restore the queue and figure out what happened to the build that was
    running when the server stopped: still running (adopt it), finished while
    we were down (adopt it; it'll be finalized right away), or gone (e.g. the
    Mac rebooted) → interrupted."""
    global _REATTACH
    if not ACTIVE_PATH.exists():
        return
    try:
        data = json.loads(ACTIVE_PATH.read_text())
    except Exception as e:
        print(f"[ubs] ignoring unreadable {ACTIVE_PATH}: {e!r}", flush=True)
        return
    known = {b.id for b in HISTORY}
    for rec in data.get("queue") or []:
        if rec.get("id") not in known:
            b = _build_from_rec(rec); b.state = "queued"
            QUEUE.append(b)
    cur = data.get("current")
    if cur and cur.get("id") not in known:
        b = _build_from_rec(cur)
        if not b.runner_pid and _read_exit(b) is None:
            # Stopped before its runner started (e.g. during the device scan):
            # nothing ran, so just run it again first.
            b.state = "queued"; b.started_at = None
            QUEUE.appendleft(b)
        elif _read_exit(b) is not None or _runner_alive(b.runner_pid, b.id, verify_cmdline=True):
            _REATTACH = b
            print(f"[ubs] reattaching to build {b.id} ({b.project_name}), "
                  f"runner pid {b.runner_pid}", flush=True)
        else:
            b.state = "interrupted"
            b.finished_at = b.finished_at or time.time()
            _load_phases(b)
            try:
                with open(b.log_path, "a") as lf:
                    lf.write("\n=== interrupted: the build process was gone when the "
                             "server restarted (host reboot?) ===\n")
            except OSError:
                pass
            HISTORY.appendleft(b); save_history()
    if _REATTACH or QUEUE:
        _wakeup.set()


def _load_phases(b: Build) -> None:
    """Phase timings come from the runner's phases file (absolute timestamps),
    turned into offsets from started_at."""
    try:
        lines = _phases_path(b).read_text().splitlines()
    except OSError:
        return
    base = b.started_at or 0
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
            out.append({"name": r["name"], "start": max(0.0, r["t"] - base)})
        except (ValueError, KeyError, TypeError):
            continue
    if out or not b.phases:
        b.phases = out


def find_build(build_id: str) -> Optional[Build]:
    if CURRENT and CURRENT.id == build_id:
        return CURRENT
    if _REATTACH and _REATTACH.id == build_id:
        return _REATTACH
    for b in list(QUEUE) + list(HISTORY):
        if b.id == build_id:
            return b
    return None


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _phase_header(line: str) -> str:
    """build.sh's log() prints '>>> <title>' in bold; return the title or ''."""
    ln = _ANSI_RE.sub("", line).strip()
    return ln[4:].strip()[:160] if ln.startswith(">>> ") else ""


# Unity prints an "Asset Pipeline Refresh" summary with top-level categories on
# single-tab lines ("\tImportOutOfDateAssets: 2260517.905ms"); children have
# two or more tabs and are already included in their parent's figure.
_UNITY_CAT_RE = re.compile(r"^\t([A-Za-z][A-Za-z0-9]+): ([0-9.]+)ms")


def unity_breakdown(log_path: Path) -> list:
    """Sum Unity's top-level asset-pipeline timings across the whole build log.
    Works on old logs too (no live timestamps needed). Stops at build.sh's
    failure dump, which re-prints the tail of the Unity log and would double count."""
    totals: dict = {}
    try:
        with open(log_path, errors="replace") as f:
            for line in f:
                if "Last lines of the Unity log" in line:
                    break
                m = _UNITY_CAT_RE.match(line)
                if m:
                    totals[m.group(1)] = totals.get(m.group(1), 0.0) + float(m.group(2)) / 1000.0
    except OSError:
        return []
    rows = [{"name": k, "seconds": round(v, 1)} for k, v in totals.items() if v >= 0.5]
    return sorted(rows, key=lambda r: -r["seconds"])
# --------------------------------------------------------------------------- #
# Device targeting — which devices a project may / does deploy to
# --------------------------------------------------------------------------- #


def _device_name(dev_id: str) -> str:
    d = DEVICES.get(dev_id)
    return (d.get("name") or dev_id) if d else dev_id


def allowed_kinds(p: Project) -> list[str]:
    """Device kinds this project may deploy to. A Quest project with
    target_kinds ["quest"] never installs onto an Android phone, and vice versa."""
    plat = p.platform if p.platform in devmod.PLATFORM_KINDS else "ios"
    every = devmod.PLATFORM_KINDS[plat]
    ks = [k for k in (p.target_kinds or []) if k in every]
    return ks or list(every)


def is_compatible(p: Project, d: dict) -> bool:
    plat = p.platform if p.platform in devmod.PLATFORM_KINDS else "ios"
    return d.get("platform") == plat and d.get("kind") in allowed_kinds(p)


def default_device_ids(p: Project) -> list[str]:
    """The project's default targets. Falls back to the legacy single
    device_udid (mapped to an inventory id when the device is known)."""
    if p.deploy_devices:
        return list(p.deploy_devices)
    if p.device_udid:
        d = DEVICES.find(p.device_udid)
        return [d["id"] if d else p.device_udid]
    return []


def validate_device_ids(p: Project, ids) -> list[str]:
    """Explicitly picked devices must exist and match the project's kinds."""
    if ids is None:
        return []
    if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids):
        raise HTTPException(status_code=400, detail="devices must be a list of device ids")
    out = []
    for x in ids:
        d = DEVICES.get(x)
        if not d:
            raise HTTPException(status_code=400, detail=f"unknown device {x!r} — scan devices first")
        if not is_compatible(p, d):
            want = "/".join(devmod.KIND_LABEL.get(k, k) for k in allowed_kinds(p))
            raise HTTPException(status_code=400, detail=(
                f"{d.get('name')} is {'an' if str(d.get('kind_label','')).lower()[:1] in 'aeiou' else 'a'} "
                f"{d.get('kind_label')}; {p.name} deploys to {want} only"))
        if x not in out:
            out.append(x)
    return out


async def resolve_deploy_targets(build: "Build", project: Project) -> tuple[list[str], list[str], list[str]]:
    """At run time: (device ids, adb serials / UDIDs to hand the build script,
    log notes). Offline or incompatible devices are skipped with a note, so one
    headset being asleep never blocks the install to the others."""
    ids = build.devices or default_device_ids(project)
    notes: list[str] = []
    if not ids:
        return [], [], ["no deploy targets: pick devices on Install / Build to…, "
                        "or set default targets in the project's settings"]
    await DEVICES.scan(max_age=0)        # fresh: reconnects WiFi headsets too
    ok_ids, transports = [], []
    for x in ids:
        d = DEVICES.get(x)
        if d is None:
            # Legacy raw serial / host:port the inventory hasn't identified yet:
            # pass it through as before rather than silently dropping it.
            if x == project.device_udid:
                ok_ids.append(x); transports.append(x)
                notes.append(f"{x}: not in the device inventory — trying it as given")
            else:
                notes.append(f"{x}: unknown device, skipped")
            continue
        label = f"{d.get('name')} ({d.get('kind_label')})"
        if not is_compatible(project, d):
            notes.append(f"{label}: not a target for this project, skipped")
        elif not d.get("online"):
            notes.append(f"{label}: {d.get('state') or 'offline'} — skipped (Install it later)")
        else:
            ok_ids.append(x); transports.append(d.get("transport") or d.get("udid") or x)
            notes.append(f"{label}: online via {d.get('connection') or '?'} → {transports[-1]}")
    return ok_ids, transports, notes


async def run_build(build: Build, project: Project) -> None:
    global CURRENT
    CURRENT = build
    build.state = "running"
    build.started_at = time.time()
    env = os.environ.copy()
    # Per-build output directory (on the USB if OUTPUT_ROOT points there).
    out_dir = OUTPUT_ROOT / project.id / build.id
    out_dir.mkdir(parents=True, exist_ok=True)
    build.output_dir = str(out_dir)
    # Only the enabled pre-build steps, in order, as JSON for the build script.
    steps = [s for s in project.pre_build_steps if s.get("enabled", True)]
    # Claim a monotonic build number for a REAL build (not install/distribute,
    # which reuse an existing artifact's number). The build script injects this
    # into PlayerSettings (iOS buildNumber / Android bundleVersionCode) before
    # the player build, so stores never see a duplicate.
    is_real_build = build.source not in ("install", "distribute")
    if is_real_build:
        bn = reg.claim_build_number(project.id)
        build.build_number = str(bn) if bn else ""
    platform = project.platform if project.platform in PLATFORMS else "ios"
    build_target, build_method = PLATFORMS[platform]
    store_pass, key_pass = (_keystore_passwords(project.keystore_path)
                            if platform == "android" else ("", ""))
    # Deploy targets (install runs always deploy). Resolved now, not at enqueue,
    # so a headset that woke up while the build was queued is still included.
    target_notes: list[str] = []
    transports: list[str] = []
    if build.deploy and build.source != "distribute":
        build.deployed_to, transports, target_notes = await resolve_deploy_targets(build, project)
    # Per-project parameters passed via env, never shell interpolation.
    env.update({
        "UBS_BUILD_ID": build.id,
        "UBS_PROJECT_DIR": project.local_path,
        "UBS_BRANCH": project.branch,
        "UBS_DEPLOY": "1" if build.deploy else "0",
        # Platform-derived: build.sh passes UBS_BUILD_TARGET to every Unity
        # launch (pre-build steps included) and branches its post-Unity stage on
        # UBS_PLATFORM. CIBuild.SetScriptingDefines also reads UBS_PLATFORM.
        "UBS_PLATFORM": platform,
        "UBS_BUILD_TARGET": build_target,
        "UBS_BUILD_METHOD": build_method,
        # Android signing/output. Passwords come from host config, not the registry.
        "UBS_ANDROID_AAB": "1" if project.android_build_aab else "0",
        "UBS_KEYSTORE_PATH": str(Path(project.keystore_path).expanduser()) if project.keystore_path else "",
        "UBS_KEYSTORE_ALIAS": project.keystore_alias,
        "UBS_KEYSTORE_PASS": store_pass,
        "UBS_KEY_ALIAS_PASS": key_pass,
        "UBS_ADB": _adb_path() if platform == "android" else "",
        "UBS_DEVELOPMENT_TEAM": project.development_team,
        "UBS_BUNDLE_ID": project.bundle_id,
        # Every online target (space-separated adb serials / iOS UDIDs); the
        # build script installs to each. UBS_DEVICE_UDID = the first, for
        # single-device code paths.
        "UBS_DEVICES": " ".join(transports),
        "UBS_DEVICE_UDID": transports[0] if transports else "",
        "UBS_RECLAIM_DISK": "1" if project.reclaim_disk else "0",
        "UBS_CLEAN_IGNORED": "1" if project.clean_ignored else "0",
        "UBS_OUTPUT_DIR": str(out_dir),
        "UBS_XCODE_BUILD_DIR": str(out_dir / "xcode"),
        "UBS_PRE_BUILD_STEPS": json.dumps(steps),
        # Per-project CCD/content values, for pre-build steps to reference.
        # The ugs service-account auth itself stays in the shared host env.
        "UBS_CCD_PROJECT_ID": project.ccd_project_id,
        "UBS_CCD_ENVIRONMENT": project.ccd_environment,
        "UBS_CCD_BUCKET": project.ccd_bucket,
        "UBS_ADDRESSABLES_PROFILE": project.addressables_profile,
        "UBS_REMOTE_BUILD_PATH": project.remote_build_path,
        "UBS_SCRIPTING_DEFINES": project.scripting_defines,
        "UBS_MERGE_FROM": project.merge_from,
        # Build number to stamp into PlayerSettings (empty for install/distribute).
        "UBS_BUILD_NUMBER": build.build_number,
        # Install-only run (from the UI "Install" button): push an existing
        # artifact to the phone without rebuilding.
        "UBS_INSTALL_ONLY": "1" if build.source == "install" else "0",
        "UBS_INSTALL_ARTIFACT": build.artifact or "",
        # Distribute-only run (from the UI "Distribute" button): export an
        # existing archive for App Store and upload to TestFlight.
        "UBS_DISTRIBUTE": "1" if build.source == "distribute" else "0",
        "UBS_DISTRIBUTE_ARCHIVE": build.artifact if build.source == "distribute" else "",
        "UBS_ASC_KEY_ID": ASC_KEY_ID,
        "UBS_ASC_ISSUER_ID": ASC_ISSUER_ID,
        "UBS_ASC_KEY_PATH": ASC_KEY_PATH,
    })
    # Run a snapshot of scripts/, not the live copy: bash reads a script as it
    # goes, so editing/pulling build.sh mid-build could otherwise change (or
    # break) a build that's already running. The snapshot lives with the output.
    script = BUILD_SCRIPT
    try:
        snap = out_dir / ".ubs-scripts"
        shutil.copytree(Path(BUILD_SCRIPT).parent, snap, dirs_exist_ok=True)
        script = str(snap / Path(BUILD_SCRIPT).name)
    except Exception as e:
        target_notes.append(f"(could not snapshot build scripts, running live copy: {e!r})")
    with open(build.log_path, "w") as logf:
        logf.write(f"=== build {build.id} | {project.name} | {time.ctime()} ===\n")
        if target_notes:
            logf.write("=== deploy targets ===\n")
            for n in target_notes:
                logf.write(f"    {n}\n")
    for stale in (_phases_path(build), _exit_path(build)):
        stale.unlink(missing_ok=True)
    if build.state == "cancelled":       # cancelled while devices were scanned
        finalize_build(build, project, None)
        return
    proc = None
    try:
        # The runner (app/runner.py) owns build.sh's output pipe and records
        # phases + exit code to files, so this server can stop/restart without
        # touching the build. It gets its own session (setsid), so it's also
        # the process group cancel signals — Unity/xcodebuild included.
        # Plain Popen, NOT asyncio: asyncio kills its subprocesses when the
        # event loop closes, which is exactly what we're avoiding.
        with open(build.log_path, "ab") as lf:   # runner's own errors, if any
            proc = subprocess.Popen(
                [sys.executable, "-I", str(RUNNER), build.id, str(build.log_path),
                 str(_phases_path(build)), str(_exit_path(build)),
                 "--", "/bin/bash", script],
                stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, env=env,
                start_new_session=True, close_fds=True)
        build.runner_pid = proc.pid
        save_active()
        rc = await supervise(build, proc)
    except Exception as e:
        finalize_build(build, project, -1, error=f"server error starting build: {e!r}")
        return
    finalize_build(build, project, rc)


async def supervise(build: Build, proc: Optional[subprocess.Popen] = None) -> Optional[int]:
    """Follow a build's runner until it ends; return the build's exit code, or
    None if the runner vanished without recording one. `proc` is set when we
    started the runner ourselves (so we can reap it); after a restart we only
    have its pid."""
    phases_mtime = 0.0
    while True:
        try:
            mt = _phases_path(build).stat().st_mtime
            if mt != phases_mtime:
                phases_mtime = mt
                _load_phases(build)
        except OSError:
            pass
        ex = _read_exit(build)
        if ex is not None:
            if proc is not None:
                await asyncio.to_thread(proc.wait)
            _load_phases(build)
            return int(ex.get("rc", -1))
        gone = (proc.poll() is not None) if proc is not None else \
            not _runner_alive(build.runner_pid, build.id)
        if gone:
            ex = _read_exit(build)     # it may have written it just before exiting
            _load_phases(build)
            if ex is not None:
                return int(ex.get("rc", -1))
            return proc.returncode if proc is not None else None
        await asyncio.sleep(1.0)


def finalize_build(build: Build, project: Optional[Project], rc: Optional[int],
                   error: str = "") -> None:
    """Record the outcome of a build (fresh or reattached) and run the
    post-build steps: artifact, version info, history, retention, auto-distribute.
    rc None = the runner vanished without recording an exit code."""
    global CURRENT
    CURRENT = build
    build.returncode = rc
    with open(build.log_path, "a") as logf:
        if build.state == "cancelled":
            logf.write(f"\n=== cancelled by user (rc={rc}) ===\n")
        elif error:
            build.state = "failed"
            logf.write(f"\n=== {error} ===\n")
        elif rc is None:
            build.state = "interrupted"
            logf.write("\n=== interrupted: the build process ended without reporting "
                       "an exit code ===\n")
        else:
            build.state = "success" if rc == 0 else "failed"
            logf.write(f"\n=== finished rc={rc} ({build.state}) ===\n")
    build.finished_at = build.finished_at or time.time()
    ex = _read_exit(build)
    if ex and ex.get("finished_at"):
        build.finished_at = float(ex["finished_at"])   # true end, even if we were down
    dur = build.duration()
    if dur is not None:
        with open(build.log_path, "a") as lf:
            lf.write(f"=== duration: {dur:.0f}s ===\n")
    for f in (_phases_path(build), _exit_path(build)):
        f.unlink(missing_ok=True)
    platform = build.platform if build.platform in PLATFORMS else "ios"
    # Find the artifact the build produced (ipa > archive > app on iOS; apk > aab on Android).
    build.artifact = _find_artifact(Path(build.output_dir), platform) if build.output_dir else ""
    # Read back version/commit the build script recorded (build-info.json in the
    # output dir). Best-effort: absence just leaves the fields blank.
    if build.output_dir:
        info_path = Path(build.output_dir) / "build-info.json"
        try:
            if info_path.exists():
                info = json.loads(info_path.read_text())
                build.version = str(info.get("version", "") or build.version)
                # The script echoes back the applied build number; trust it over
                # our claimed one if present (they should match).
                build.build_number = str(info.get("build_number", "") or build.build_number)
                build.commit_sha = str(info.get("commit_sha", "") or build.commit_sha)
        except Exception:
            pass
        # build-info.json is only written after Unity succeeds; commit.txt is
        # written right after the git sync, so failed builds still say which
        # commit they attempted.
        if not build.commit_sha:
            try:
                build.commit_sha = (Path(build.output_dir) / "commit.txt").read_text().strip()[:12]
            except OSError:
                pass
    HISTORY.appendleft(build)
    save_history()
    prune_outputs(build.project_id)
    CURRENT = None
    save_active()
    # The repo status panel compares against the last build; refresh it now.
    schedule_repo_status(build.project_id, force=True)
    # Auto-distribute: a successful webhook build of a project that opted in gets
    # queued for TestFlight upload (only if ASC is configured). Never auto-distribute
    # install/distribute runs themselves, or manual/failed builds.
    if (project and build.state == "success" and build.source == "gitea" and platform == "ios"
            and project.auto_distribute and ASC_KEY_ID and ASC_ISSUER_ID and ASC_KEY_PATH):
        archive = _find_archive(build)
        if archive:
            nb = enqueue(project, "distribute", False)
            if nb:
                nb.artifact = archive
                save_active()


_ARTIFACT_PATTERNS = {
    "ios": ("**/*.ipa", "**/*.xcarchive", "**/Release-iphoneos/*.app"),
    # Unity writes the APK/AAB directly — no second toolchain. Only look in the
    # android/ subdir so stray .apk files elsewhere (e.g. inside an embedded SDK
    # cache) can't be mistaken for the build output.
    "android": ("android/*.apk", "android/*.aab"),
}


def _find_artifact(out_dir: Path, platform: str = "ios") -> str:
    if not out_dir.exists():
        return ""
    for pattern in _ARTIFACT_PATTERNS.get(platform, _ARTIFACT_PATTERNS["ios"]):
        hits = sorted(out_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if hits:
            return str(hits[0])
    return ""


def _find_archive(build: "Build") -> str:
    """The .xcarchive for a build, needed for distribution. Prefer the build's
    output dir; fall back to the stored artifact if it's already an archive."""
    if build.output_dir:
        hits = sorted(Path(build.output_dir).glob("**/*.xcarchive"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if hits:
            return str(hits[0])
    if build.artifact and build.artifact.endswith(".xcarchive") and Path(build.artifact).exists():
        return build.artifact
    return ""


def avg_duration(project_id: str, n: int = 5) -> Optional[float]:
    """Mean duration of the last up-to-n SUCCESSFUL builds of a project, for ETA.
    Only successful builds count — a build that failed fast would skew the ETA low."""
    durs = []
    for b in HISTORY:
        if b.project_id == project_id and b.state == "success":
            d = b.duration()
            if d and d > 0:
                durs.append(d)
        if len(durs) >= n:
            break
    return sum(durs) / len(durs) if durs else None


def prune_outputs(project_id: str) -> None:
    """Keep only the most recent KEEP_BUILDS_PER_PROJECT output dirs for a project."""
    if KEEP_BUILDS_PER_PROJECT <= 0:
        return
    proj_dir = OUTPUT_ROOT / project_id
    if not proj_dir.exists():
        return
    try:
        dirs = sorted([d for d in proj_dir.iterdir() if d.is_dir()],
                      key=lambda d: d.stat().st_mtime, reverse=True)
        for old in dirs[KEEP_BUILDS_PER_PROJECT:]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception:
        pass


async def _adopt(build: Build) -> None:
    """Finish following a build that was running before the server restarted."""
    global CURRENT
    CURRENT = build
    save_active()
    try:
        rc = await supervise(build)
    except Exception as e:
        print(f"[ubs] lost track of reattached build {build.id}: {e!r}", flush=True)
        rc = None
    finalize_build(build, reg.get(build.project_id), rc)


async def worker_loop() -> None:
    global _REATTACH
    if _REATTACH is not None:
        b, _REATTACH = _REATTACH, None
        await _adopt(b)
    while True:
        if not QUEUE:
            _wakeup.clear(); await _wakeup.wait(); continue
        build = QUEUE.popleft()
        project = reg.get(build.project_id)
        if not project or not project.ready:
            build.state = "failed"
            build.log_path.write_text("project no longer exists\n" if not project
                                      else "project is not onboarded yet (clone unfinished)\n")
            HISTORY.appendleft(build); save_history(); save_active(); continue
        try:
            await run_build(build, project)
        except Exception as e:
            # Never let one bad build stop the queue.
            print(f"[ubs] build {build.id} crashed the worker: {e!r}", flush=True)
            if CURRENT is build:
                finalize_build(build, project, -1, error=f"server error: {e!r}")


# --------------------------------------------------------------------------- #
# Clone onboarding (background, survives refreshes and restarts)
# --------------------------------------------------------------------------- #

# project id -> the asyncio task downloading/verifying it. The registry record
# (onboard_state) is the source of truth; this only tracks what's running now.
ONBOARD_TASKS: "dict[str, asyncio.Task]" = {}


def _onboard_log(project_id: str) -> Path:
    return LOG_DIR / f"onboard-{project_id}.log"


def _start_onboarding(project_id: str) -> asyncio.Task:
    t = ONBOARD_TASKS.get(project_id)
    if t and not t.done():
        return t
    t = asyncio.create_task(onboarding.finish_clone_onboarding(
        reg, project_id, _onboard_log(project_id)))
    ONBOARD_TASKS[project_id] = t
    t.add_done_callback(lambda _t, pid=project_id:
                        ONBOARD_TASKS.pop(pid, None) if ONBOARD_TASKS.get(pid) is _t else None)
    return t


def _require_ready(proj: Project) -> None:
    if not proj.ready:
        what = "still being cloned" if proj.onboard_state == "cloning" else "not onboarded yet (failed — Retry it)"
        raise HTTPException(status_code=409, detail=f"{proj.name} is {what}")


@app.on_event("startup")
async def _startup() -> None:
    load_history()
    load_active()      # queue + the build that was running before the restart
    asyncio.create_task(worker_loop())
    asyncio.create_task(hoststats.sampler(_build_pid))
    # A clone that was mid-download when the server stopped picks up where it
    # left off (the project dir and record are already there).
    for p in reg.list():
        if p.onboard_state == "cloning":
            _start_onboarding(p.id)


def enqueue(project: Project, source: str, deploy: bool, coalesce: bool = False,
            devices: Optional[list] = None):
    """Add a build to the queue. With coalesce=True (webhook pushes), if this
    project already has a build WAITING in the queue, don't add another — the
    pending build will fetch the latest origin state when it runs, so a second
    queued build would just be a redundant rebuild of the same commits. Rapid
    pushes thus collapse to 'at most one running + one pending' per project.
    Manual builds (coalesce=False) always queue, since a deliberate re-tap means
    the user wants another run."""
    if coalesce:
        for q in QUEUE:
            if q.project_id == project.id:
                return None   # already a pending build for this project
    b = Build(id=uuid.uuid4().hex[:12], project_id=project.id,
              project_name=project.name, source=source, deploy=deploy,
              platform=project.platform or "ios", devices=list(devices or []))
    QUEUE.append(b); _wakeup.set()
    save_active()
    return b


async def _json_body(request: Request) -> dict:
    """Optional JSON body: {} when absent/empty (keeps old clients working)."""
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json body")
    return body if isinstance(body, dict) else {}


def _check_token(token: str) -> None:
    if not BUILD_TOKEN or not hmac.compare_digest(token, BUILD_TOKEN):
        raise HTTPException(status_code=401, detail="bad or missing build token")


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


# --------------------------------------------------------------------------- #
# Project management API
# --------------------------------------------------------------------------- #


@app.get("/api/projects")
async def api_projects():
    out = []
    for p in reg.list():
        d = p.public()
        if not p.ready:
            d["onboard_running"] = p.id in ONBOARD_TASKS
            d["onboard_progress"] = onboarding.read_progress(_onboard_log(p.id))
        out.append(d)
    return out


# How long POST /clone waits for git before answering. Bad URLs, auth failures
# and unknown branches fail inside this window and come back as a plain 400;
# anything still downloading after it is answered with the "cloning" record and
# carries on in the background.
CLONE_FAST_FAIL_S = 8


@app.post("/api/projects/clone")
async def api_clone(request: Request, x_build_token: str = Header(default="")):
    _check_token(x_build_token)
    body = await request.json()
    name = (body.get("name") or "").strip()
    repo_url = (body.get("repo_url") or "").strip()
    branch = (body.get("branch") or "main").strip()
    if not name or not repo_url:
        raise HTTPException(status_code=400, detail="name and repo_url required")
    platform = _check_platform(body.get("platform") or "ios")
    dest = onboarding.clone_dest(BUILDS_ROOT, name)
    for other in reg.list():
        if other.local_path and Path(other.local_path).resolve() == dest.resolve():
            raise HTTPException(status_code=400, detail=(
                f"{dest} is already used by project '{other.name}'. "
                "Use a different name for a second project on the same repo."))
    try:
        # True = a clone of this repo is already there (e.g. left by an earlier,
        # interrupted onboarding): it gets reused and only topped up.
        existed = onboarding.check_clone_dest(dest, repo_url)
    except onboarding.OnboardError as e:
        raise HTTPException(status_code=400, detail=str(e))
    dest.mkdir(parents=True, exist_ok=True)
    # Register BEFORE downloading: from here on the project survives a page
    # refresh or a server restart, and the download resumes on startup.
    proj = reg.add(Project(
        id=Registry.new_id(), name=name, repo_url=repo_url, branch=branch,
        local_path=str(dest), platform=platform,
        onboard_state="cloning", onboard_started_at=time.time()))
    task = _start_onboarding(proj.id)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=CLONE_FAST_FAIL_S)
    except asyncio.TimeoutError:
        pass   # still downloading — that's the normal case for a real repo
    cur = reg.get(proj.id)
    if cur and cur.onboard_state == "failed" and not existed:
        # Failed straight away (typo'd URL, no access, wrong branch): undo it so
        # the user can just fix the form and resubmit.
        reg.remove(proj.id)
        _remove_clone_dir(dest)
        raise HTTPException(status_code=400, detail=cur.onboard_error or "clone failed")
    return (cur or proj).public()


@app.post("/api/projects/{project_id}/retry-onboard")
async def api_retry_onboard(project_id: str, x_build_token: str = Header(default="")):
    """Restart a failed (or orphaned) clone; resumes from what's on disk."""
    _check_token(x_build_token)
    proj = reg.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="no such project")
    if proj.ready:
        return proj.public()
    proj = reg.update(project_id, onboard_state="cloning", onboard_error="")
    _start_onboarding(project_id)
    return proj.public()


@app.get("/api/projects/{project_id}/onboard-log", response_class=PlainTextResponse)
async def api_onboard_log(project_id: str):
    lp = _onboard_log(project_id)
    if not lp.exists():
        return ""
    # git progress overwrites itself with \r; keep only the final state of each line.
    text = lp.read_text(errors="replace")[-200_000:]
    return "\n".join(ln.rsplit("\r", 1)[-1] for ln in text.split("\n"))


def _remove_clone_dir(p: Path) -> None:
    """Delete a clone, but only ever one under builds_root."""
    p = Path(p).resolve()
    root = BUILDS_ROOT.resolve()
    if p != root and root in p.parents and p.exists():
        shutil.rmtree(p, ignore_errors=True)


def _check_platform(p: str) -> str:
    p = (p or "").strip().lower()
    if p not in PLATFORMS:
        raise HTTPException(status_code=400, detail=f"platform must be one of {sorted(PLATFORMS)}")
    return p


@app.post("/api/projects/adopt")
async def api_adopt(request: Request, x_build_token: str = Header(default="")):
    _check_token(x_build_token)
    body = await request.json()
    name = (body.get("name") or "").strip()
    local_path = (body.get("local_path") or "").strip()
    if not name or not local_path:
        raise HTTPException(status_code=400, detail="name and local_path required")
    # Keep adoptions inside the browse sandbox.
    rp = Path(local_path).resolve()
    if not str(rp).startswith(str(BROWSE_ROOT.resolve())):
        raise HTTPException(status_code=400, detail="path outside the allowed root")
    platform = _check_platform(body.get("platform") or "ios")
    try:
        proj = onboarding.onboard_from_path(reg, name, str(rp))
    except onboarding.OnboardError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if platform != proj.platform:
        proj = reg.update(proj.id, platform=platform)
    return proj.public()


@app.patch("/api/projects/{project_id}")
async def api_edit(project_id: str, request: Request, x_build_token: str = Header(default="")):
    _check_token(x_build_token)
    body = await request.json()
    allowed = {"name", "branch", "development_team", "bundle_id", "device_udid",
               "webhook_deploys", "reclaim_disk", "clean_ignored", "pre_build_steps",
               "ccd_project_id", "ccd_environment", "ccd_bucket",
               "addressables_profile", "remote_build_path", "scripting_defines",
               "merge_from", "auto_distribute",
               "platform", "android_build_aab", "keystore_path", "keystore_alias",
               "target_kinds", "deploy_devices"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if "platform" in fields:
        fields["platform"] = _check_platform(fields["platform"])
    cur = reg.get(project_id)
    if not cur:
        raise HTTPException(status_code=404, detail="no such project")
    plat = fields.get("platform", cur.platform)
    if "target_kinds" in fields:
        tk = fields["target_kinds"]
        every = devmod.PLATFORM_KINDS.get(plat, [])
        if not isinstance(tk, list) or any(k not in every for k in tk):
            raise HTTPException(status_code=400, detail=f"target_kinds must be a subset of {every}")
    if "deploy_devices" in fields:
        # Validate against the project AS IT WILL BE (new platform / kinds).
        future = Project.from_record({**cur.public(), **fields})
        fields["deploy_devices"] = validate_device_ids(future, fields["deploy_devices"])
    proj = reg.update(project_id, **fields)
    if "branch" in fields or "merge_from" in fields:
        REPO_STATUS.pop(project_id, None)      # compared against the wrong branches now
        schedule_repo_status(project_id, force=True)
    return proj.public()


@app.get("/api/projects/{project_id}/branches")
async def api_branches(project_id: str, x_build_token: str = Header(default="")):
    """List remote branches via git ls-remote, so the UI can offer a dropdown."""
    _check_token(x_build_token)
    proj = reg.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="no such project")
    try:
        out = subprocess.run(
            ["git", "ls-remote", "--heads", proj.repo_url],
            capture_output=True, text=True, timeout=20,
            cwd=proj.local_path or None,
        )
    except Exception as e:
        return {"branches": [], "error": str(e)}
    if out.returncode != 0:
        return {"branches": [], "error": out.stderr.strip() or "git ls-remote failed"}
    branches = []
    for line in out.stdout.splitlines():
        # "<sha>\trefs/heads/<branch>"
        if "refs/heads/" in line:
            branches.append(line.split("refs/heads/", 1)[1].strip())
    return {"branches": sorted(set(branches))}


@app.post("/api/projects/{project_id}/verify")
async def api_verify(project_id: str, x_build_token: str = Header(default="")):
    _check_token(x_build_token)
    proj = reg.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="no such project")
    _require_ready(proj)
    try:
        info = onboarding.verify_project(Path(proj.local_path))
    except onboarding.OnboardError as e:
        raise HTTPException(status_code=400, detail=str(e))
    proj = reg.update(project_id, **{k: info[k] for k in
                      ("unity_version", "build_script_installed")})
    return proj.public()


@app.delete("/api/projects/{project_id}")
async def api_delete(project_id: str, delete_files: bool = Query(default=False),
                     x_build_token: str = Header(default="")):
    _check_token(x_build_token)
    proj = reg.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="no such project")
    # Stop an in-flight clone first (kills git) so it can't recreate files or
    # write the record back after we remove it.
    task = ONBOARD_TASKS.pop(project_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    if delete_files and proj.local_path:
        # Only delete clones we made under builds_root — never an adopted path elsewhere.
        _remove_clone_dir(Path(proj.local_path))
    reg.remove(project_id)
    try:
        _onboard_log(project_id).unlink()
    except OSError:
        pass
    return {"removed": True}


# --------------------------------------------------------------------------- #
# Device inventory API
# --------------------------------------------------------------------------- #


@app.get("/api/devices")
async def api_devices(refresh: bool = Query(default=False)):
    """Every known device with live state. Scans adb + devicectl (cached a few
    seconds, so the UI can poll); refresh=1 forces a fresh scan."""
    devs = await DEVICES.scan(max_age=0 if refresh else 5)
    return {"devices": devs, "errors": DEVICES.last_errors,
            "kinds": devmod.KIND_LABEL, "platform_kinds": devmod.PLATFORM_KINDS}


@app.get("/api/projects/{project_id}/devices")
async def api_project_devices(project_id: str, refresh: bool = Query(default=False)):
    """Devices this project can deploy to (platform + target_kinds), online
    first, with the project's defaults flagged — feeds the UI's device picker."""
    proj = reg.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="no such project")
    devs = await DEVICES.scan(max_age=0 if refresh else 5)
    defaults = set(default_device_ids(proj))
    out = [{**d, "default": d["id"] in defaults} for d in devs if is_compatible(proj, d)]
    hidden = sum(1 for d in devs if not is_compatible(proj, d))
    return {"devices": out, "allowed_kinds": allowed_kinds(proj),
            "incompatible_count": hidden, "errors": DEVICES.last_errors}


@app.patch("/api/devices/{dev_id}")
async def api_device_rename(dev_id: str, request: Request, x_build_token: str = Header(default="")):
    _check_token(x_build_token)
    body = await _json_body(request)
    d = DEVICES.rename(dev_id, str(body.get("name", "")))
    if not d:
        raise HTTPException(status_code=404, detail="no such device")
    return d


@app.delete("/api/devices/{dev_id}")
async def api_device_forget(dev_id: str, x_build_token: str = Header(default="")):
    """Forget a device (it reappears on the next scan if still attached).
    Also drops it from every project's default targets."""
    _check_token(x_build_token)
    if not DEVICES.forget(dev_id):
        raise HTTPException(status_code=404, detail="no such device")
    for p in reg.list():
        if dev_id in (p.deploy_devices or []):
            reg.update(p.id, deploy_devices=[x for x in p.deploy_devices if x != dev_id])
    return {"removed": True}


def _check_address(addr: str) -> str:
    addr = (addr or "").strip()
    if not devmod.ADDRESS_RE.match(addr):
        raise HTTPException(status_code=400, detail="address must look like 192.168.1.20:5555")
    return addr


@app.post("/api/devices/connect")
async def api_device_connect(request: Request, x_build_token: str = Header(default="")):
    """`adb connect ip:port` — for a headset already in tcpip mode, or one
    that's paired for Wireless Debugging but not advertising over mDNS."""
    _check_token(x_build_token)
    addr = _check_address((await _json_body(request)).get("address", ""))
    rc, out = await devmod._run([_adb_path(), "connect", addr], timeout=10)
    devs = await DEVICES.scan(max_age=0)
    ok = rc == 0 and ("connected" in out and "fail" not in out.lower())
    return {"ok": ok, "output": out.strip(), "devices": devs}


@app.post("/api/devices/pair")
async def api_device_pair(request: Request, x_build_token: str = Header(default="")):
    """`adb pair ip:port code` — one-time Wireless Debugging pairing. On the
    headset/phone: Developer options → Wireless debugging → Pair device with
    pairing code shows the address (its port is the PAIRING port, not the
    connect port) and a 6-digit code. After this, the device is found by mDNS
    and reconnected automatically on every scan, even after reboots."""
    _check_token(x_build_token)
    body = await _json_body(request)
    addr = _check_address(body.get("address", ""))
    code = str(body.get("code", "")).strip()
    if not code.isdigit() or not 4 <= len(code) <= 10:
        raise HTTPException(status_code=400, detail="pairing code is the 6 digits shown on the device")
    rc, out = await devmod._run([_adb_path(), "pair", addr, code], timeout=20)
    ok = rc == 0 and "Successfully paired" in out
    devs = await DEVICES.scan(max_age=0) if ok else DEVICES.list()
    return {"ok": ok, "output": out.strip(), "devices": devs}


async def _log_target(proj: Project, device: str) -> str:
    """The adb serial / UDID to read logs from: the picked device, else the
    project's single online default. 4xx with a readable reason otherwise."""
    if device:
        d = DEVICES.get(device)
        if not d or not is_compatible(proj, d):
            raise HTTPException(status_code=400, detail="that device isn't a target for this project")
    else:
        ids = default_device_ids(proj)
        if not ids:
            raise HTTPException(status_code=404, detail="no device picked and no default target set")
        await DEVICES.scan()
        online = [DEVICES.get(x) for x in ids]
        online = [d for d in online if d and d.get("online")]
        if not online:
            # Legacy raw serial the inventory doesn't know: use it as before.
            if proj.device_udid and not DEVICES.find(proj.device_udid):
                return proj.device_udid
            raise HTTPException(status_code=409, detail="none of this project's target devices are online")
        d = online[0]
    await DEVICES.scan()
    d = DEVICES.get(d["id"]) or d
    return d.get("transport") or d.get("udid") or d["id"]


# --------------------------------------------------------------------------- #
# Server-side directory browser (folder picker over the MINI's filesystem)
# --------------------------------------------------------------------------- #


@app.get("/api/browse")
async def api_browse(path: str = Query(default=""), x_build_token: str = Header(default="")):
    _check_token(x_build_token)
    root = BROWSE_ROOT.resolve()
    target = (root if not path else Path(path)).resolve()
    # Never escape the sandbox root.
    if not str(target).startswith(str(root)):
        target = root
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="not a directory")
    entries = []
    try:
        for child in sorted(target.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                is_unity = (child / "ProjectSettings" / "ProjectVersion.txt").exists()
                entries.append({"name": child.name, "path": str(child),
                                "is_unity_project": is_unity})
    except PermissionError:
        raise HTTPException(status_code=403, detail="permission denied")
    parent = str(target.parent) if target != root else None
    return {"root": str(root), "path": str(target), "parent": parent, "entries": entries}


# --------------------------------------------------------------------------- #
# Builds
# --------------------------------------------------------------------------- #


@app.post("/build/{project_id}")
async def trigger_build(project_id: str, request: Request, x_build_token: str = Header(default=""),
                        deploy: Optional[bool] = Query(default=None)):
    _check_token(x_build_token)
    proj = reg.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="no such project")
    _require_ready(proj)
    do_deploy = True if deploy is None else deploy
    # Optional body {"devices": [...]} — "Build to…" in the UI. Omitted = defaults.
    body = await _json_body(request)
    devs = validate_device_ids(proj, body.get("devices"))
    return JSONResponse(enqueue(proj, "phone", do_deploy, devices=devs).public())


@app.post("/cancel/{build_id}")
async def cancel_build(build_id: str, x_build_token: str = Header(default="")):
    """Cancel a build: if it's running, kill its whole process group (Unity and
    xcodebuild included); if it's only queued, drop it from the queue."""
    _check_token(x_build_token)
    # Case 1: the running build.
    if CURRENT and CURRENT.id == build_id:
        cur = CURRENT
        cur.state = "cancelled"          # finalize_build sees this and labels it
        save_active()                    # a restart mid-cancel stays cancelled
        pid = cur.runner_pid
        # The runner is a session leader: its pid is the process group that
        # holds build.sh, Unity and xcodebuild. Works for reattached builds too.
        if _runner_alive(pid, cur.id):
            try:
                os.killpg(pid, signal.SIGTERM)           # ask the whole group to stop
                # Escalate to SIGKILL shortly if it's still alive.
                async def _hard_kill(b, g):
                    await asyncio.sleep(8)
                    if _read_exit(b) is None and _runner_alive(g, b.id):
                        try: os.killpg(g, signal.SIGKILL)
                        except ProcessLookupError: pass
                asyncio.create_task(_hard_kill(cur, pid))
            except ProcessLookupError:
                pass
        return {"cancelling": True, "build_id": build_id}
    # Case 2: a queued build — just remove it.
    for b in list(QUEUE):
        if b.id == build_id:
            b.state = "cancelled"
            try: QUEUE.remove(b)
            except ValueError: pass
            b.log_path.write_text("=== cancelled before it started ===\n")
            HISTORY.appendleft(b); save_history(); save_active()
            return {"cancelled": True, "build_id": build_id}
    raise HTTPException(status_code=404, detail="build not running or queued")


@app.post("/webhook/gitea")
async def gitea_webhook(request: Request, x_gitea_signature: str = Header(default="")):
    body = await request.body()
    if GITEA_WEBHOOK_SECRET:
        expected = hmac.new(GITEA_WEBHOOK_SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_gitea_signature):
            raise HTTPException(status_code=401, detail="bad webhook signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")

    # Route to the project whose repo URL matches the push.
    repo = payload.get("repository", {})
    repo_url = repo.get("clone_url") or repo.get("html_url") or ""
    ref = payload.get("ref", "")
    branch = ref.rsplit("/", 1)[-1] if ref else ""

    # One repo can back several projects (ios + android on their own branches,
    # both merging from main), so a push fans out to EVERY matching project
    # whose trigger branch it hit. They queue and run one after another.
    projs = reg.list_by_repo(repo_url)
    if not projs:
        return JSONResponse({"skipped": True, "reason": f"no project matches {repo_url}"})
    after = str(payload.get("after") or "")
    results = []
    for p in projs:
        r = _webhook_for_project(p, branch)
        _record_hook(p.id, branch, after, r)
        results.append(r)
    if len(results) == 1:
        return JSONResponse(results[0])
    return JSONResponse({"projects": results})


def _webhook_for_project(proj: Project, branch: str) -> dict:
    if not proj.ready:
        return {"project": proj.name, "skipped": True,
                "reason": f"project is not onboarded yet ({proj.onboard_state})"}
    # Decide which branch pushes trigger a build:
    #  - If merge_from is set (e.g. ios merges main), the TRIGGER is the merge_from
    #    branch (main). We deliberately do NOT trigger on pushes to the build
    #    branch itself, because the build pushes the merged result back to
    #    origin/<build branch> — triggering on that would queue a redundant
    #    second build (a ripple). So a human push to main builds; the machine's
    #    merge-back push to ios does not.
    #  - If merge_from is NOT set, trigger on the build branch as before.
    if proj.merge_from:
        triggers = {proj.merge_from}
    else:
        triggers = {proj.branch}
    if branch and branch not in triggers:
        return {"project": proj.name, "skipped": True,
                "reason": f"push to '{branch}', this project builds on "
                          + ("/".join(sorted(triggers)))}
    # Coalesce rapid pushes: don't stack redundant builds for the same project.
    b = enqueue(proj, "gitea", proj.webhook_deploys, coalesce=True)
    if b is None:
        return {"project": proj.name, "coalesced": True,
                "reason": "a build for this project is already queued; "
                          "it will pick up the latest commits"}
    return b.public()


# --------------------------------------------------------------------------- #
# Repo status: last build / last success / freshness vs origin + auto-sync parent
# --------------------------------------------------------------------------- #

# Last push webhook received per project, so the UI can tell "no push arrived"
# (webhook never fired / failed to deliver) from "push arrived but was ignored".
HOOKS_PATH = LOG_DIR / "webhooks.json"
LAST_HOOKS: dict = {}
try:
    LAST_HOOKS = json.loads(HOOKS_PATH.read_text()) if HOOKS_PATH.exists() else {}
except Exception:
    LAST_HOOKS = {}


def _record_hook(project_id: str, branch: str, after: str, result: dict) -> None:
    if result.get("skipped"):
        outcome = "ignored: " + str(result.get("reason", ""))
    elif result.get("coalesced"):
        outcome = "coalesced into the queued build"
    else:
        outcome = "queued build " + str(result.get("id", ""))
    LAST_HOOKS[project_id] = {"time": time.time(), "branch": branch,
                              "after": after[:40], "outcome": outcome[:200]}
    try:
        tmp = HOOKS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(LAST_HOOKS, indent=2))
        os.replace(tmp, HOOKS_PATH)
    except Exception:
        pass


REPO_STATUS: dict = {}                     # project id -> repostatus.compute() result
REPO_STATUS_TASKS: "dict[str, asyncio.Task]" = {}
REPO_STATUS_TTL = float(CFG.get("repo_status_ttl_s", 120))
_repo_status_sem = asyncio.Semaphore(2)    # don't ls-remote every repo at once


def _is_real_build(b: Build) -> bool:
    return b.source not in ("install", "distribute")


def build_summary(p: Project) -> dict:
    """Last build / last successful build of a project, from history (newest first)."""
    last = last_ok = None
    for b in HISTORY:
        if b.project_id != p.id or not _is_real_build(b):
            continue
        if last is None:
            last = b
        if b.state == "success":
            last_ok = b
            break
    running = CURRENT if (CURRENT and CURRENT.project_id == p.id) else None
    queued = [b for b in QUEUE if b.project_id == p.id]

    def brief(b: Optional[Build]) -> Optional[dict]:
        if not b:
            return None
        d = b.public()
        return {k: d.get(k) for k in ("id", "state", "source", "created_at", "created_iso",
                                      "finished_at", "elapsed", "version_label", "commit_short",
                                      "has_artifact", "artifact_url", "label", "returncode")}
    return {"last": brief(last), "last_success": brief(last_ok),
            "running": brief(running), "queued": len(queued),
            "last_hook": LAST_HOOKS.get(p.id)}


async def _refresh_repo_status(p: Project) -> None:
    built = ""
    attempted = ""
    for b in HISTORY:   # newest first; short shas are resolved by git
        if b.project_id == p.id and _is_real_build(b) and b.commit_sha:
            if not attempted:
                attempted = b.commit_sha
            if b.state == "success":
                built = b.commit_sha
                break
    busy = bool(CURRENT and CURRENT.project_id == p.id)
    async with _repo_status_sem:
        try:
            REPO_STATUS[p.id] = await repostatus.compute(
                p.local_path, p.branch, p.merge_from, built_sha=built,
                attempted_sha=attempted, allow_fetch=not busy)
        except Exception as e:   # never let the panel take the server down
            REPO_STATUS[p.id] = {"checked_at": time.time(), "error": f"status check failed: {e!r}"}


def schedule_repo_status(project_id: str, force: bool = False) -> bool:
    """Start a background refresh if the cached one is old (or force). Returns
    True if a refresh is now running."""
    t = REPO_STATUS_TASKS.get(project_id)
    if t and not t.done():
        return True
    p = reg.get(project_id)
    if not p or not p.ready:
        return False
    cached = REPO_STATUS.get(project_id)
    if not force and cached and time.time() - cached.get("checked_at", 0) < REPO_STATUS_TTL:
        return False
    try:
        t = asyncio.get_running_loop().create_task(_refresh_repo_status(p))
    except RuntimeError:
        return False
    REPO_STATUS_TASKS[project_id] = t
    return True


@app.get("/api/projects/status")
async def api_projects_status(refresh: bool = False, project_id: str = ""):
    """Per-project status for the cards: build summary (live, from history) plus
    the git freshness check (cached for REPO_STATUS_TTL seconds and refreshed in
    the background — the response never waits on git). ?refresh=1 forces a
    re-check; poll again a few seconds later to see the result."""
    out = {}
    for p in reg.list():
        if project_id and p.id != project_id:
            continue
        if not p.ready:
            continue
        d = build_summary(p)
        d["checking"] = schedule_repo_status(p.id, force=refresh)
        d["git"] = REPO_STATUS.get(p.id)
        d["branch"] = p.branch
        d["merge_from"] = p.merge_from
        d["trigger_branch"] = p.merge_from or p.branch
        out[p.id] = d
    return out


def _disk_free_gb(path: Path) -> Optional[float]:
    try:
        st = os.statvfs(str(path))
        return round(st.f_bavail * st.f_frsize / 1e9, 1)
    except Exception:
        return None


def _dir_size_bytes(path: Path) -> int:
    """Total size of a directory tree, following no symlinks. Cheap-ish; build
    output dirs are big but not pathologically deep."""
    total = 0
    try:
        for root_, _dirs, files in os.walk(path, followlinks=False):
            for fn in files:
                fp = os.path.join(root_, fn)
                try:
                    total += os.lstat(fp).st_size
                except OSError:
                    pass
    except Exception:
        pass
    return total


@app.get("/api/disk")
async def api_disk():
    """Disk usage report: the volume holding build output (total/used/free) plus
    a per-project, per-build breakdown of how much the historical builds occupy.
    Used by the UI's storage panel and to decide whether there's room for more
    projects. Sizes are measured live from the filesystem."""
    # Volume stats for the output location.
    vol = {"path": str(OUTPUT_ROOT)}
    try:
        st = os.statvfs(str(OUTPUT_ROOT))
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        vol.update({
            "total_gb": round(total / 1e9, 1),
            "free_gb": round(free / 1e9, 1),
            "used_gb": round((total - free) / 1e9, 1),
            "used_pct": round((total - free) / total * 100) if total else None,
        })
    except Exception as e:
        vol["error"] = repr(e)

    # Per-project build output sizes. Walk OUTPUT_ROOT/<project-id>/<build-id>/.
    projects = []
    builds_total = 0
    names = {p.id: p.name for p in reg.list()}
    try:
        for proj_dir in sorted(OUTPUT_ROOT.iterdir()):
            if not proj_dir.is_dir():
                continue
            pid = proj_dir.name
            build_rows = []
            proj_bytes = 0
            for bdir in sorted(proj_dir.iterdir()):
                if not bdir.is_dir():
                    continue
                sz = _dir_size_bytes(bdir)
                proj_bytes += sz
                try:
                    mt = bdir.stat().st_mtime
                except OSError:
                    mt = 0
                build_rows.append({
                    "build_id": bdir.name,
                    "bytes": sz,
                    "mb": round(sz / 1e6, 1),
                    "mtime": mt,
                    "mtime_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(mt)) if mt else "",
                })
            builds_total += proj_bytes
            build_rows.sort(key=lambda r: r["bytes"], reverse=True)
            projects.append({
                "project_id": pid,
                "project_name": names.get(pid, pid),
                "bytes": proj_bytes,
                "gb": round(proj_bytes / 1e9, 2),
                "build_count": len(build_rows),
                "builds": build_rows,
            })
    except Exception as e:
        return {"volume": vol, "error": repr(e), "projects": []}

    projects.sort(key=lambda p: p["bytes"], reverse=True)
    return {
        "volume": vol,
        "builds_total_bytes": builds_total,
        "builds_total_gb": round(builds_total / 1e9, 2),
        "project_count": len(projects),
        "projects": projects,
    }


@app.get("/status")
async def status():
    cur = None
    if CURRENT:
        cur = CURRENT.public()
        cur["avg_duration"] = avg_duration(CURRENT.project_id)   # for ETA/progress
    # Free space where build outputs land, so the UI can warn before it fills.
    free = _disk_free_gb(OUTPUT_ROOT)
    return {
        "current": cur,
        "queued": [b.public() for b in QUEUE],
        "history": [b.public() for b in list(HISTORY)[:10]],
        "disk_free_gb": free,
        "disk_low": (free is not None and free < 5),   # warn under 5 GB
    }


# Host utilisation widget (see app/hoststats.py). Watch the volumes builds use.
hoststats.VOLUMES = [OUTPUT_ROOT, BUILDS_ROOT, LOG_DIR]


def _build_pid() -> int:
    b = CURRENT
    return b.runner_pid if (b and b.state in ("running", "cancelled")) else 0


@app.get("/api/host")
async def api_host():
    """CPU / memory / disk / I/O of the build machine, plus what the running
    build's process tree uses, with ~10 min of history for sparklines."""
    snap = hoststats.snapshot()
    snap["building"] = bool(CURRENT)
    snap["build_project"] = CURRENT.project_name if CURRENT else ""
    snap["queued"] = len(QUEUE)
    return snap


@app.get("/logs/{build_id}/raw", response_class=PlainTextResponse)
async def logs_raw(build_id: str):
    b = find_build(build_id)
    if not b or not b.log_path.exists():
        raise HTTPException(status_code=404, detail="no such build/log")
    return b.log_path.read_text(errors="replace")


@app.get("/api/builds/{build_id}/timing")
async def build_timing(build_id: str):
    """Where the time went: build.sh phases (recorded live; builds from before
    this feature have none) plus Unity's own asset-pipeline categories."""
    b = find_build(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="no such build")
    return {"total": b.duration(), "phases": b.timing(),
            "unity": await asyncio.to_thread(unity_breakdown, b.log_path)}


@app.get("/logs/{build_id}")
async def logs_stream(build_id: str, request: Request):
    """SSE live log. Every event carries the byte offset just past its line as
    its id, so when the connection drops (e.g. the server restarts mid-build)
    the browser's automatic reconnect sends Last-Event-ID and the stream
    resumes exactly there instead of replaying the whole log."""
    b = find_build(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="no such build")
    try:
        start = max(0, int(request.headers.get("last-event-id") or 0))
    except ValueError:
        start = 0

    async def gen():
        pos = start
        buf = b""
        if start:
            yield "retry: 2000\n\n"
        while True:
            p = b.log_path
            finished = b.state in ("success", "failed", "cancelled", "interrupted")
            if p.exists():
                size = p.stat().st_size
                if size < pos:            # log was rewritten (shouldn't happen); start over
                    pos, buf = 0, b""
                if size > pos:
                    with open(p, "rb") as f:
                        f.seek(pos); data = f.read(size - pos)
                    pos += len(data)
                    buf += data
                    # Only whole lines, so a reconnect never splits one; a giant
                    # unterminated line (IL2CPP) is flushed once it's huge.
                    *lines, buf = buf.split(b"\n")
                    if len(buf) > 262144:
                        lines.append(buf); buf = b""
                    off = pos - len(buf)
                    for i, line in enumerate(lines):
                        txt = line.decode(errors="replace").rstrip("\r")
                        # id only on the last event of the batch (offset after it)
                        if i == len(lines) - 1:
                            yield f"id: {off}\ndata: {txt}\n\n"
                        else:
                            yield f"data: {txt}\n\n"
            if finished and (not p.exists() or p.stat().st_size <= pos):
                if buf:
                    yield f"id: {pos}\ndata: {buf.decode(errors='replace')}\n\n"
                yield f"event: done\ndata: {b.state}\n\n"; break
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- #
# Android device logs (adb logcat). Same three UI buttons as iOS:
#   live (relaunch + stream), last run (dump the ring buffer, no relaunch), zip.
# --------------------------------------------------------------------------- #

# Unity's Debug.Log goes to the "Unity" tag; Java crashes to AndroidRuntime;
# native crashes (IL2CPP) to DEBUG/libc/CRASH. Everything else silenced.
_LOGCAT_FILTER = ["Unity:V", "AndroidRuntime:E", "DEBUG:V", "libc:E", "CRASH:V",
                  "ActivityManager:W", "*:S"]


def _adb_cmd(serial: str, *args: str) -> list[str]:
    return [_adb_path(), "-s", serial, *args]


async def _adb_connect_if_wifi(serial: str) -> str:
    """ADB-over-WiFi: a serial of host:port needs `adb connect` first (after a
    one-time `adb tcpip 5555` over USB). No-op for USB serials."""
    if ":" not in serial:
        return ""
    _rc, out = await _run_capture([_adb_path(), "connect", serial], timeout=15)
    return out


async def _sse_proc(cmd: list[str], cap_s: int = 0):
    """Run cmd, yield its output as SSE data lines (chunked, long-line safe).
    cap_s > 0 stops after that many seconds (for endless streams like logcat)."""
    yield f"data: $ {' '.join(cmd)}\n\n"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True, limit=1024 * 1024)
    except FileNotFoundError:
        yield f"data: (command not found: {cmd[0]} — install android-platform-tools "\
              "or set adb_path in config.json)\n\n"
        return
    start = time.time()
    buf = b""
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(16384), timeout=5)
            except asyncio.TimeoutError:
                chunk = None
            if chunk == b"":
                break
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    one, buf = buf.split(b"\n", 1)
                    yield f"data: {one.decode(errors='replace').rstrip(chr(13))}\n\n"
            if cap_s and time.time() - start > cap_s:
                yield f"data: --- stopped after {cap_s // 60} min; tap again to resume ---\n\n"
                break
        if buf:
            yield f"data: {buf.decode(errors='replace')}\n\n"
    finally:
        if proc.returncode is None:
            try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception: pass


def _android_device_logs(proj: Project, serial: str):
    pkg = proj.bundle_id

    async def gen():
        note = await _adb_connect_if_wifi(serial)
        if note:
            yield f"data: {note.strip()}\n\n"
        # Clear the ring buffer so the stream starts at THIS launch.
        await _run_capture(_adb_cmd(serial, "logcat", "-c"), timeout=15)
        if pkg:
            yield f"data: launching {pkg}…\n\n"
            # force-stop first so this is a fresh process (matches iOS relaunch).
            await _run_capture(_adb_cmd(serial, "shell", "am", "force-stop", pkg), timeout=15)
            _rc, out = await _run_capture(_adb_cmd(
                serial, "shell", "monkey", "-p", pkg, "-c",
                "android.intent.category.LAUNCHER", "1"), timeout=20)
            if "No activities found" in out or "monkey aborted" in out:
                yield f"data: (could not launch {pkg} — is it installed? {out.strip()})\n\n"
        else:
            yield "data: (no package name set — streaming without relaunch)\n\n"
        async for line in _sse_proc(_adb_cmd(serial, "logcat", "-v", "time", *_LOGCAT_FILTER),
                                    cap_s=180):
            yield line
        yield "event: done\ndata: done\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


def _android_last_logs(proj: Project, serial: str):

    async def gen():
        yield "data: === Last-run logs from the device ring buffer (no relaunch) ===\n\n"
        note = await _adb_connect_if_wifi(serial)
        if note:
            yield f"data: {note.strip()}\n\n"
        yield "data: \n\n"
        yield "data: --- Unity + crash tags (logcat -d) ---\n\n"
        async for line in _sse_proc(_adb_cmd(serial, "logcat", "-d", "-v", "time", *_LOGCAT_FILTER)):
            yield line
        yield "data: \n\n"
        yield "data: --- crash buffer (logcat -b crash) ---\n\n"
        async for line in _sse_proc(_adb_cmd(serial, "logcat", "-d", "-b", "crash", "-v", "time")):
            yield line
        yield "data: \n\n"
        yield "data: (logcat is a rolling buffer — a long session after the failure "\
              "can push it out. Use ⤓ Logs zip for the full buffer.)\n\n"
        yield "event: done\ndata: done\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


async def _android_logs_zip(proj: Project, serial: str):
    import tempfile, zipfile
    pkg = proj.bundle_id
    work = tempfile.mkdtemp(prefix="logzip-")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    manifest = [f"Unity build server — Android log bundle",
                f"project: {proj.name}  ({proj.id})", f"device:  {serial}",
                f"app:     {pkg}", f"created: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}", ""]
    try:
        zippath = os.path.join(work, f"devicelogs-{proj.id}-{stamp}.zip")
        with zipfile.ZipFile(zippath, "w", zipfile.ZIP_DEFLATED) as zf:
            note = await _adb_connect_if_wifi(serial)
            if note:
                zf.writestr("commands/adb-connect.txt", note)
            for name, args in (("logcat-unity.txt", ["logcat", "-d", "-v", "time", *_LOGCAT_FILTER]),
                               ("logcat-all.txt", ["logcat", "-d", "-v", "threadtime"]),
                               ("logcat-crash.txt", ["logcat", "-d", "-b", "crash", "-v", "time"])):
                rc, out = await _run_capture(_adb_cmd(serial, *args), timeout=120)
                zf.writestr(name, out)
                manifest.append(f"{name}: {'ok' if rc == 0 else f'rc={rc}'}")
            # App-written logs under persistentDataPath (/sdcard/Android/data/<pkg>/files).
            if pkg:
                dest = os.path.join(work, "appdata")
                os.makedirs(dest, exist_ok=True)
                rc, out = await _run_capture(_adb_cmd(
                    serial, "pull", f"/sdcard/Android/data/{pkg}/files", dest), timeout=180)
                zf.writestr("commands/appdata-pull.txt", out)
                added = 0
                for root_, _d, files in os.walk(dest):
                    for fn in files:
                        low = fn.lower()
                        if low.endswith((".log", ".txt")) or "log" in low:
                            fp = os.path.join(root_, fn)
                            zf.write(fp, os.path.join("app-logs", os.path.relpath(fp, dest)))
                            added += 1
                manifest.append(f"app-logs: {added} file(s) from persistentDataPath")
            zf.writestr("manifest.txt", "\n".join(manifest) + "\n")
        out_zip = os.path.join(tempfile.gettempdir(), os.path.basename(zippath))
        shutil.move(zippath, out_zip)
        return FileResponse(out_zip, filename=os.path.basename(out_zip),
                            media_type="application/zip")
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.get("/device-logs/{project_id}")
async def device_logs(project_id: str, device: str = ""):
    """Stream the device console (xcrun devicectl) for a project's target device,
    so the app's runtime output (addressables errors, exceptions) is visible in
    the UI. Filtered to the app's process when possible. SSE, like build logs.
    Android projects stream adb logcat instead. ?device=<id> picks the device."""
    proj = reg.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="no such project")
    target = await _log_target(proj, device)
    if proj.platform == "android":
        return _android_device_logs(proj, target)

    if not proj.bundle_id:
        raise HTTPException(status_code=400, detail="project has no bundle id to launch")

    async def gen():
        # devicectl has NO `console` command. The reliable way to see a Unity
        # app's Debug.Log output is to LAUNCH it with --console attached, which
        # streams the app's stdout/stderr as it runs. This also (re)launches the
        # app, so the user doesn't launch it separately.
        # Correct, verified form: launch with --console attaches the app's
        # stdout/stderr (Unity Debug.Log). devicectl has no standalone console
        # command, so launching with --console is the supported way to see output.
        cmd = ["xcrun", "devicectl", "device", "process", "launch",
               "--console", "--device", target, proj.bundle_id]
        yield f"data: launching {proj.bundle_id} with console attached…\n\n"
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                limit=1024 * 1024)   # tolerate long lines (see build reader note)
        except Exception as e:
            yield f"data: could not launch with console: {e!r}\n\n"
            yield "event: done\ndata: error\n\n"; return
        try:
            import time as _t
            start = _t.time()
            assert proc.stdout is not None
            # Chunked read so an over-long console line can't kill the stream.
            buf = b""
            while True:
                chunk = await proc.stdout.read(16384)
                if not chunk:
                    if buf:
                        yield f"data: {buf.decode(errors='replace')}\n\n"
                    break
                buf += chunk
                while b"\n" in buf:
                    one, buf = buf.split(b"\n", 1)
                    yield f"data: {one.decode(errors='replace')}\n\n"
                if _t.time() - start > 180:   # 3 min cap
                    yield "data: --- stopped after 3 min; tap again to resume ---\n\n"
                    break
        finally:
            try:
                import os, signal
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass
        yield "event: done\ndata: done\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


async def _run_capture(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    """Run a command, return (returncode, combined stdout+stderr). Used by the
    log-zip endpoint, which doesn't stream — it just collects everything."""
    header = "$ " + " ".join(cmd) + "\n"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            limit=1024 * 1024)
    except FileNotFoundError:
        return 127, header + f"(command not found: {cmd[0]})\n"
    except Exception as e:
        return 1, header + f"(failed to run: {e!r})\n"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            import os as _o, signal as _s
            _o.killpg(_o.getpgid(proc.pid), _s.SIGTERM)
        except Exception:
            pass
        return 124, header + "(timed out)\n"
    return proc.returncode or 0, header + (out.decode(errors="replace") if out else "")


@app.get("/device-logs-zip/{project_id}")
async def device_logs_zip(project_id: str, minutes: int = 30, device: str = ""):
    """Collect ALL last-run log sources into a single downloadable zip:
      - the app-written log file(s) from the data container (the full file, not
        the truncated live view),
      - crash reports (.ips) matching the app, plus an index of all crash files,
      - the unified log (if `log collect` works on this host), and
      - a manifest.txt describing what was and wasn't collected.
    This is the right way to grab a long play session's logs — it can be many MB,
    so it's a file download, not a stream. Does NOT relaunch the app.
    """
    proj = reg.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="no such project")
    udid = await _log_target(proj, device)
    if proj.platform == "android":
        return await _android_logs_zip(proj, udid)
    proc_name = (getattr(proj, "device_process_name", "") or "").strip()
    if not proc_name and proj.bundle_id:
        proc_name = proj.bundle_id.split(".")[-1]

    import tempfile, os as _os, shutil as _sh, zipfile, re as _re, time as _t
    work = tempfile.mkdtemp(prefix="logzip-")
    manifest: list[str] = []
    stamp = _t.strftime("%Y%m%d-%H%M%S")

    def note(s: str):
        manifest.append(s)

    try:
        note(f"Unity build server — last-run log bundle")
        note(f"project: {proj.name}  ({proj.id})")
        note(f"device:  {udid}")
        note(f"app:     {proj.bundle_id}  (process ~{proc_name})")
        note(f"created: {_t.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        note("")

        zippath = _os.path.join(work, f"devicelogs-{proj.id}-{stamp}.zip")
        zf = zipfile.ZipFile(zippath, "w", zipfile.ZIP_DEFLATED)

        # --- 1. App container: pull it and add every log/txt file --------------
        if proj.bundle_id:
            contdir = _os.path.join(work, "container")
            _os.makedirs(contdir, exist_ok=True)
            rc, out = await _run_capture(
                ["xcrun", "devicectl", "device", "copy", "from",
                 "--device", udid, "--domain-type", "appDataContainer",
                 "--domain-identifier", proj.bundle_id, "--user", "mobile",
                 "--source", ".", "--destination", contdir], timeout=180)
            zf.writestr("commands/container-pull.txt", out)
            added = 0
            for root_, _dirs, files in _os.walk(contdir):
                for fn in files:
                    low = fn.lower()
                    if (low.endswith(".log") or low.endswith(".txt")
                            or "log" in low or "player" in low or "output" in low):
                        fp = _os.path.join(root_, fn)
                        rel = _os.path.relpath(fp, contdir)
                        try:
                            zf.write(fp, _os.path.join("app-logs", rel))
                            added += 1
                        except Exception as e:
                            note(f"  (couldn't add {rel}: {e!r})")
            note(f"app-logs: {added} file(s) from the data container"
                 + ("" if added else "  — NONE (app writes no log file on iOS by default)"))
        else:
            note("app-logs: skipped (project has no bundle id)")

        # --- 2. Crash reports --------------------------------------------------
        crashdir = _os.path.join(work, "crashes")
        _os.makedirs(crashdir, exist_ok=True)
        rc, out = await _run_capture(
            ["xcrun", "devicectl", "device", "copy", "from",
             "--device", udid, "--domain-type", "systemCrashLogs",
             "--source", ".", "--destination", crashdir], timeout=120)
        zf.writestr("commands/crash-pull.txt", out)
        core = _re.sub(r"[^A-Za-z0-9]", "", proc_name).lower() if proc_name else ""
        allcrash, appcrash = [], 0
        for root_, _dirs, files in _os.walk(crashdir):
            for fn in files:
                if not (fn.endswith(".ips") or fn.endswith(".crash") or fn.endswith(".panic")):
                    continue
                allcrash.append(fn)
                fp = _os.path.join(root_, fn)
                # App-matching crashes go in crashes/app/, the rest in crashes/all/.
                if core and core in _re.sub(r"[^A-Za-z0-9]", "", fn).lower():
                    zf.write(fp, _os.path.join("crashes", "app", fn)); appcrash += 1
                else:
                    zf.write(fp, _os.path.join("crashes", "all", fn))
        zf.writestr("crashes/INDEX.txt", "\n".join(sorted(allcrash, reverse=True)))
        note(f"crashes: {len(allcrash)} total on device, {appcrash} matching the app")

        # --- 3. Unified log (best-effort; often fails on Apple Silicon/USB) ----
        archive = _os.path.join(work, "unified.logarchive")
        rc, out = await _run_capture(
            ["sudo", "-n", "log", "collect", "--device-udid", udid,
             "--last", f"{minutes}m", "--output", archive], timeout=180)
        zf.writestr("commands/unified-collect.txt", out)
        if _os.path.isdir(archive):
            # Export a filtered text view so you don't need Console to read it.
            pred = (f'process CONTAINS[c] "{core}" OR processImagePath CONTAINS[c] "{core}"'
                    if core else 'eventType == logEvent')
            rc2, out2 = await _run_capture(
                ["log", "show", "--archive", archive, "--predicate", pred,
                 "--info", "--debug", "--style", "compact"], timeout=180)
            zf.writestr("unified-log.txt", out2)
            # Also zip the raw archive for Console.app.
            for root_, _dirs, files in _os.walk(archive):
                for fn in files:
                    fp = _os.path.join(root_, fn)
                    rel = _os.path.relpath(fp, _os.path.dirname(archive))
                    zf.write(fp, _os.path.join("unified", rel))
            note(f"unified-log: collected ({minutes}m lookback); see unified-log.txt")
        else:
            note("unified-log: NOT collected (log collect failed — see "
                 "commands/unified-collect.txt. On Apple Silicon over USB this "
                 "commonly fails with 'Device not configured'. The app-logs above "
                 "are the primary source.)")

        zf.writestr("manifest.txt", "\n".join(manifest) + "\n")
        zf.close()

        # Move the zip somewhere it survives the finally-cleanup of `work`.
        out_zip = _os.path.join(tempfile.gettempdir(), _os.path.basename(zippath))
        _sh.move(zippath, out_zip)
        return FileResponse(out_zip, filename=_os.path.basename(out_zip),
                            media_type="application/zip")
    finally:
        _sh.rmtree(work, ignore_errors=True)


@app.get("/device-logs-last/{project_id}")
async def device_logs_last(project_id: str, minutes: int = 10, device: str = ""):
    """Retrieve the LAST run's logs from the device WITHOUT relaunching the app.

    Unlike /device-logs (which launches with --console and only shows the run it
    starts), this pulls what iOS has already stored:
      1. The unified log (os_log) — captured into a .logarchive with `log collect`
         and read back filtered to the app process. This holds Unity Debug.Log /
         Addressables errors from the previous run, as long as they're still in
         the device's rolling buffer (hence the `minutes` lookback).
      2. Any recent crash reports (.ips) for the app, pulled via devicectl.
    Neither step starts the app, so a crash log survives and is grabbable after
    the fact. Streamed as SSE so the (slow) `log collect` shows progress.
    """
    proj = reg.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="no such project")
    udid = await _log_target(proj, device)
    if proj.platform == "android":
        return _android_last_logs(proj, udid)

    # Unity's player process name is the product name, not the bundle id. Default
    # to the bundle id's last path component if no explicit process name is set.
    proc_name = (getattr(proj, "device_process_name", "") or "").strip()
    if not proc_name and proj.bundle_id:
        proc_name = proj.bundle_id.split(".")[-1]

    async def run(cmd: list[str], timeout: int = 300):
        """Run a command, stream stdout/stderr lines. Returns returncode."""
        yield f"data: $ {' '.join(cmd)}\n\n"
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                limit=1024 * 1024)
        except FileNotFoundError:
            yield f"data: (command not found: {cmd[0]})\n\n"
            return
        except Exception as e:
            yield f"data: (failed to run: {e!r})\n\n"
            return
        import time as _t
        start = _t.time()
        assert proc.stdout is not None
        buf = b""
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(16384), timeout=5)
            except asyncio.TimeoutError:
                if _t.time() - start > timeout:
                    yield "data: (timed out)\n\n"
                    try:
                        import os, signal
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except Exception:
                        pass
                    break
                continue
            if not chunk:
                if buf:
                    yield f"data: {buf.decode(errors='replace')}\n\n"
                break
            buf += chunk
            while b"\n" in buf:
                one, buf = buf.split(b"\n", 1)
                yield f"data: {one.decode(errors='replace')}\n\n"
        await proc.wait()

    async def gen():
        import tempfile, os as _os, shutil as _sh
        workdir = tempfile.mkdtemp(prefix="devlogs-")
        archive = _os.path.join(workdir, "last.logarchive")
        try:
            yield f"data: === Fetching last-run logs for {proc_name or '(app)'} (no relaunch) ===\n\n"
            yield f"data: device {udid}, looking back {minutes} min\n\n"

            # --- 1. Unified log snapshot (does NOT launch the app) -------------
            # `log collect` grabs the device's in-memory log into a .logarchive.
            # Collecting from an ATTACHED DEVICE requires root, so we run it via
            # `sudo -n` (non-interactive). If passwordless sudo isn't set up for
            # this one command, that fails cleanly and we tell the user the exact
            # sudoers line to add (see the message below).
            yield "data: \n\n"
            yield "data: --- collecting unified log (needs sudo; ~15-30s) ---\n\n"
            collected = True
            async for line in run(
                ["sudo", "-n", "log", "collect", "--device-udid", udid,
                 "--last", f"{minutes}m", "--output", archive],
                timeout=180):
                yield line
            if not _os.path.isdir(archive):
                collected = False
                yield "data: (unified-log collect didn't produce an archive.)\n\n"
                yield "data: 'log collect' needs root for an attached device. Enable it "\
                      "once (no password prompt afterwards) by running on the mini:\n\n"
                yield "data:     sudo ./dev/enable-devicelogs-sudo.sh\n\n"
                yield "data: That installs a sudoers rule for ONLY 'log collect'. The crash "\
                      "pull below needs no root and still works.\n\n"

            # --- 2. Read it back, filtered to the app process -----------------
            if collected:
                yield "data: \n\n"
                yield f"data: --- app log (~{proc_name}) from the archive ---\n\n"
                # Match forgivingly: the Unity process name is the PRODUCT name
                # (e.g. "VoxelMage"), which may differ from the bundle-id tail
                # (e.g. "Voxel-Mage") by punctuation/casing. Strip non-alphanumerics
                # and match on CONTAINS so either spelling hits. Falls back to all
                # log events if we have no name at all.
                import re as _re
                core = _re.sub(r"[^A-Za-z0-9]", "", proc_name) if proc_name else ""
                if core:
                    pred = (f'process CONTAINS[c] "{core}" '
                            f'OR processImagePath CONTAINS[c] "{core}"')
                else:
                    pred = 'eventType == logEvent'
                async for line in run(
                    ["log", "show", "--archive", archive,
                     "--predicate", pred, "--info", "--debug",
                     "--style", "compact"],
                    timeout=120):
                    yield line

            # --- 2c. Hunt for an app-written log file in the container ---------
            # Unity on iOS logs to the device console by default (no file), but an
            # app CAN write its own log (e.g. via Application.logMessageReceived)
            # into its data container. We don't know where, so pull the WHOLE
            # container and walk it for .log/.txt files. No root needed.
            if proj.bundle_id:
                yield "data: \n\n"
                yield "data: --- hunting for an app-written log file in the container ---\n\n"
                contdir = _os.path.join(workdir, "container")
                _os.makedirs(contdir, exist_ok=True)
                async for line in run(
                    ["xcrun", "devicectl", "device", "copy", "from",
                     "--device", udid,
                     "--domain-type", "appDataContainer",
                     "--domain-identifier", proj.bundle_id,
                     "--user", "mobile",
                     "--source", ".",
                     "--destination", contdir],
                    timeout=180):
                    yield line
                # Find candidate log files: by extension, or named like a log.
                logcands = []
                for root_, _dirs, files in _os.walk(contdir):
                    for fn in files:
                        low = fn.lower()
                        if (low.endswith(".log") or low.endswith(".txt")
                                or "log" in low or "player" in low or "output" in low):
                            fp = _os.path.join(root_, fn)
                            rel = _os.path.relpath(fp, contdir)
                            try:
                                sz = _os.path.getsize(fp); mt = _os.path.getmtime(fp)
                            except OSError:
                                sz, mt = 0, 0
                            logcands.append((mt, sz, rel, fp))
                logcands.sort(reverse=True)   # newest first
                if not logcands:
                    yield "data: (no .log/.txt files anywhere in the container. Unity "\
                          "isn't writing a Player.log here — on iOS it logs to the device "\
                          "console by default. To get a file, add a log sink in-app that "\
                          "writes to Application.persistentDataPath.)\n\n"
                else:
                    yield f"data: found {len(logcands)} candidate log file(s):\n\n"
                    for _mt, sz, rel, _fp in logcands[:30]:
                        yield f"data:   {rel}  ({sz} bytes)\n\n"
                    # Print the newest few in full (tail-limited).
                    for _mt, _sz, rel, fp in logcands[:3]:
                        yield f"data: \n\n"
                        yield f"data: ===== {rel} (last 2000 lines) =====\n\n"
                        try:
                            with open(fp, "r", errors="replace") as fh:
                                for ln in fh.read().splitlines()[-2000:]:
                                    yield f"data: {ln}\n\n"
                        except Exception as e:
                            yield f"data: (couldn't read {rel}: {e!r})\n\n"

            # --- 3. Recent crash reports (.ips) from the system crash store ----
            # The RIGHT domain for crashes is systemCrashLogs, not the app
            # container. This pulls recent .ips crash reports (device-wide).
            yield "data: \n\n"
            yield "data: --- recent crash reports (systemCrashLogs) ---\n\n"
            crashdir = _os.path.join(workdir, "crashes")
            _os.makedirs(crashdir, exist_ok=True)
            async for line in run(
                ["xcrun", "devicectl", "device", "copy", "from",
                 "--device", udid,
                 "--domain-type", "systemCrashLogs",
                 "--source", ".",
                 "--destination", crashdir],
                timeout=120):
                yield line
            # List EVERY crash file pulled, newest first, so you can see what's
            # there even if the name doesn't obviously match. Then print the full
            # contents of ones that match the app (Unity product name), and just
            # name the rest.
            import re as _re2
            core2 = _re2.sub(r"[^A-Za-z0-9]", "", proc_name).lower() if proc_name else ""
            crashfiles = []
            for root_, _dirs, files in _os.walk(crashdir):
                for fn in files:
                    if fn.endswith(".ips") or fn.endswith(".crash") or fn.endswith(".panic"):
                        fp = _os.path.join(root_, fn)
                        try:
                            mt = _os.path.getmtime(fp)
                        except OSError:
                            mt = 0
                        crashfiles.append((mt, fn, fp))
            crashfiles.sort(reverse=True)   # newest first

            if not crashfiles:
                yield "data: (no .ips/.crash files were pulled — the crash store is "\
                      "empty or has nothing for this device. A graceful failure like "\
                      "an HTTP 401 does NOT crash, so it won't appear here — use the "\
                      "unified log above for those.)\n\n"
            else:
                yield f"data: pulled {len(crashfiles)} crash file(s) (newest first):\n\n"
                for _mt, fn, _fp in crashfiles[:40]:
                    yield f"data:   {fn}\n\n"
                # Print full contents of app-matching reports (or all, if we have
                # no name to match and there are only a few).
                shown = 0
                for _mt, fn, fp in crashfiles:
                    nmatch = core2 and core2 in _re2.sub(r"[^A-Za-z0-9]", "", fn).lower()
                    if not (nmatch or (not core2 and len(crashfiles) <= 5)):
                        continue
                    shown += 1
                    yield f"data: \n\n"
                    yield f"data: ===== {fn} =====\n\n"
                    try:
                        with open(fp, "r", errors="replace") as fh:
                            for ln in fh.read().splitlines()[:500]:
                                yield f"data: {ln}\n\n"
                    except Exception as e:
                        yield f"data: (couldn't read {fn}: {e!r})\n\n"
                if shown == 0:
                    yield f"data: \n\n"
                    yield f"data: (none of the pulled crash files match '{proc_name}'. "\
                          "If your app crashed, it may be under a different process "\
                          "name — the list above shows everything available.)\n\n"

            yield "event: done\ndata: done\n\n"
        finally:
            try:
                _sh.rmtree(workdir, ignore_errors=True)
            except Exception:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/artifact/{build_id}")
async def download_artifact(build_id: str):
    b = find_build(build_id)
    if not b or not b.artifact or not Path(b.artifact).exists():
        raise HTTPException(status_code=404, detail="no artifact for this build")
    p = Path(b.artifact)
    # A .app or .xcarchive is a directory; zip it on the fly for download.
    if p.is_dir():
        import tempfile, zipfile
        tmp = Path(tempfile.gettempdir()) / f"{b.id}-{p.name}.zip"
        if not tmp.exists():
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for f in p.rglob("*"):
                    z.write(f, f.relative_to(p.parent))
        return FileResponse(str(tmp), filename=tmp.name)
    return FileResponse(str(p), filename=p.name)


@app.post("/build/{project_id}/install/{build_id}")
async def install_artifact(project_id: str, build_id: str, request: Request,
                           x_build_token: str = Header(default="")):
    """Re-run a device install for an already-built artifact. Body
    {"devices": [...]} picks the targets (the UI's device picker); omitted =
    the project's defaults."""
    _check_token(x_build_token)
    proj = reg.get(project_id)
    b = find_build(build_id)
    if not proj or not b or not b.artifact:
        raise HTTPException(status_code=404, detail="no such project/build/artifact")
    _require_ready(proj)
    body = await _json_body(request)
    devs = validate_device_ids(proj, body.get("devices"))
    if not devs and not default_device_ids(proj):
        raise HTTPException(status_code=400, detail="pick at least one device")
    # Enqueue a lightweight install-only build; the script sees UBS_INSTALL_ONLY.
    nb = enqueue(proj, "install", True, devices=devs)
    nb.artifact = b.artifact
    save_active()
    return JSONResponse(nb.public())


@app.post("/build/{project_id}/distribute/{build_id}")
async def distribute_build(project_id: str, build_id: str, x_build_token: str = Header(default="")):
    """Export a built archive for App Store and upload it to TestFlight."""
    _check_token(x_build_token)
    proj = reg.get(project_id)
    b = find_build(build_id)
    if not proj or not b:
        raise HTTPException(status_code=404, detail="no such project/build")
    if (b.platform or proj.platform) == "android":
        raise HTTPException(status_code=400, detail="Distribute is TestFlight-only for now. "
                            "For Android, download the APK/AAB and sideload it, upload it to "
                            "Play Console, or push it to a Quest release channel.")
    if not (ASC_KEY_ID and ASC_ISSUER_ID and ASC_KEY_PATH):
        raise HTTPException(status_code=400, detail="App Store Connect key not configured "
                            "(set asc_key_id / asc_issuer_id / asc_key_path in config.json)")
    archive = _find_archive(b)
    if not archive:
        raise HTTPException(status_code=404, detail="no .xcarchive for this build to distribute")
    nb = enqueue(proj, "distribute", False)
    nb.artifact = archive   # the build script reads this as UBS_DISTRIBUTE_ARCHIVE
    save_active()
    return JSONResponse(nb.public())


def _delete_one_build(b: "Build") -> bool:
    """Remove a build's output dir (only under OUTPUT_ROOT), its log, and its
    history entry. Never deletes a build that's currently running or queued."""
    if (CURRENT and CURRENT.id == b.id) or any(q.id == b.id for q in QUEUE):
        return False   # don't delete an in-flight build
    if b.output_dir:
        p = Path(b.output_dir).resolve()
        if str(p).startswith(str(OUTPUT_ROOT.resolve())) and p.exists():
            shutil.rmtree(p, ignore_errors=True)
    if b.log_path.exists():
        try: b.log_path.unlink()
        except OSError: pass
    try:
        HISTORY.remove(b)
    except ValueError:
        pass
    return True


@app.get("/api/builds")
async def list_builds(offset: int = 0, limit: int = 20, project_id: str = "", state: str = ""):
    """Paginated full build history (newest first). The UI pages through this;
    /status still carries just the few most-recent for the live top view.
    state=success → only successful; state=failed → failed, cancelled or
    interrupted; any other value → that exact state."""
    bad = {"failed", "cancelled", "interrupted"}
    def keep(b: Build) -> bool:
        if project_id and b.project_id != project_id:
            return False
        if state == "failed":
            return b.state in bad
        return not state or b.state == state
    items = [b for b in HISTORY if keep(b)]
    total = len(items)
    page = items[offset: offset + max(1, min(limit, 200))]
    return {"total": total, "offset": offset, "limit": limit,
            "builds": [b.public() for b in page]}


@app.get("/api/builds/{build_id}")
async def get_build(build_id: str):
    """One build (for deep links: the UI opens #build=<id>)."""
    b = find_build(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="no such build")
    return b.public()


@app.patch("/api/builds/{build_id}")
async def label_build(build_id: str, request: Request, x_build_token: str = Header(default="")):
    """Set a user-assigned label/tag on a build (e.g. 'demo for Dan', 'good RC')."""
    _check_token(x_build_token)
    b = find_build(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="no such build")
    body = await request.json()
    b.label = str(body.get("label", ""))[:120]
    save_history(); save_active()
    return b.public()


@app.delete("/api/builds/{build_id}")
async def delete_build(build_id: str, x_build_token: str = Header(default="")):
    _check_token(x_build_token)
    b = find_build(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="no such build")
    if not _delete_one_build(b):
        raise HTTPException(status_code=409, detail="build is running or queued; cancel it first")
    save_history()
    return {"removed": True}


@app.post("/api/builds/delete")
async def batch_delete_builds(request: Request, x_build_token: str = Header(default="")):
    """Delete many builds at once. Body: {"ids": [...]} to delete specific builds,
    or {"older_than_days": N} / {"keep_newest": N} / {"project_id": "..."} to
    delete by rule. In-flight builds are always skipped."""
    _check_token(x_build_token)
    body = await request.json()
    ids = set(body.get("ids") or [])
    older_than = body.get("older_than_days")
    keep_newest = body.get("keep_newest")
    project_id = body.get("project_id") or ""

    candidates = [b for b in list(HISTORY) if not project_id or b.project_id == project_id]
    to_delete = []
    if ids:
        to_delete = [b for b in candidates if b.id in ids]
    elif older_than is not None:
        cutoff = time.time() - float(older_than) * 86400
        to_delete = [b for b in candidates if b.created_at < cutoff]
    elif keep_newest is not None:
        # candidates are newest-first in HISTORY; keep the first N, delete the rest.
        to_delete = candidates[int(keep_newest):]
    else:
        raise HTTPException(status_code=400, detail="specify ids, older_than_days, or keep_newest")

    removed = 0
    for b in to_delete:
        if _delete_one_build(b):
            removed += 1
    save_history()
    return {"removed": removed, "requested": len(to_delete)}