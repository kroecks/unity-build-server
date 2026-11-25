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
  POST /api/projects/clone    onboard by cloning a repo URL
  POST /api/projects/adopt    onboard a project already on disk (path)
  PATCH /api/projects/{id}    edit Apple/behaviour fields
  DELETE /api/projects/{id}   remove (optionally delete the clone)
  POST /api/projects/{id}/verify   re-run detection + reinstall build script
  GET  /api/browse?path=...   list directories under the configured root
  POST /build/{id}            enqueue a build (token-gated)
  POST /webhook/gitea         enqueue by matching repo URL (HMAC-verified)
  GET  /status                queue + running + history
  GET  /logs/{build_id}       SSE live log
  GET  /logs/{build_id}/raw   full log text

Security unchanged from before: bind to the LAN IP, token on /build, HMAC on
the webhook, no request input is ever passed through a shell string.
"""

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Header, Query
from fastapi.responses import (HTMLResponse, JSONResponse, StreamingResponse,
                               PlainTextResponse, FileResponse)

from .registry import Registry, Project
from . import onboarding
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

reg = Registry(REGISTRY_PATH)
app = FastAPI(title="Unity Build Server")

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
        return d


QUEUE: "deque[Build]" = deque()
HISTORY: "deque[Build]" = deque(maxlen=50)
CURRENT: Optional[Build] = None
CURRENT_PROC = None          # the running build subprocess, for cancellation
_wakeup = asyncio.Event()

# Persist finished builds so history survives a server restart.
HISTORY_PATH = LOG_DIR / "history.json"


def save_history() -> None:
    try:
        data = [{"id": b.id, "project_id": b.project_id, "project_name": b.project_name,
                 "source": b.source, "deploy": b.deploy, "state": b.state,
                 "created_at": b.created_at, "started_at": b.started_at,
                 "finished_at": b.finished_at, "returncode": b.returncode,
                 "output_dir": b.output_dir, "artifact": b.artifact}
                for b in list(HISTORY)]
        tmp = HISTORY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, HISTORY_PATH)
    except Exception:
        pass  # history is a convenience; never let it break a build


def load_history() -> None:
    if not HISTORY_PATH.exists():
        return
    try:
        for rec in json.loads(HISTORY_PATH.read_text()):
            # A build left "running"/"queued" when the server died didn't finish.
            if rec.get("state") in ("running", "queued"):
                rec["state"] = "interrupted"
            HISTORY.append(Build(**{k: rec[k] for k in
                ("id", "project_id", "project_name", "source", "deploy",
                 "state", "created_at", "started_at", "finished_at",
                 "returncode", "output_dir", "artifact")
                if k in rec}))
    except Exception:
        pass


def find_build(build_id: str) -> Optional[Build]:
    if CURRENT and CURRENT.id == build_id:
        return CURRENT
    for b in list(QUEUE) + list(HISTORY):
        if b.id == build_id:
            return b
    return None


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
    # Per-project parameters passed via env, never shell interpolation.
    env.update({
        "UBS_BUILD_ID": build.id,
        "UBS_PROJECT_DIR": project.local_path,
        "UBS_BRANCH": project.branch,
        "UBS_DEPLOY": "1" if build.deploy else "0",
        "UBS_BUILD_METHOD": "BuildScript.PerformiOSBuild",
        "UBS_DEVELOPMENT_TEAM": project.development_team,
        "UBS_BUNDLE_ID": project.bundle_id,
        "UBS_DEVICE_UDID": project.device_udid,
        "UBS_RECLAIM_DISK": "1" if project.reclaim_disk else "0",
        "UBS_CLEAN_IGNORED": "1" if project.clean_ignored else "0",
        "UBS_OUTPUT_DIR": str(out_dir),
        "UBS_XCODE_BUILD_DIR": str(out_dir / "xcode"),
        "UBS_PRE_BUILD_STEPS": json.dumps(steps),
    })
    global CURRENT_PROC
    with open(build.log_path, "w") as logf:
        logf.write(f"=== build {build.id} | {project.name} | {time.ctime()} ===\n")
        logf.flush()
        try:
            # Launch in its own process group so cancel can kill Unity/xcodebuild
            # too, not just the top-level bash (which would orphan them and leave
            # the project lock held).
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash", BUILD_SCRIPT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=env,
                start_new_session=True,   # setsid: new process group
            )
            CURRENT_PROC = proc
            async for line in proc.stdout:
                logf.write(line.decode(errors="replace")); logf.flush()
            rc = await proc.wait()
            build.returncode = rc
            if build.state == "cancelled":
                logf.write(f"\n=== cancelled by user (rc={rc}) ===\n")
            else:
                build.state = "success" if rc == 0 else "failed"
                logf.write(f"\n=== finished rc={rc} ({build.state}) ===\n")
        except Exception as e:
            build.state = "failed"; build.returncode = -1
            logf.write(f"\n=== server error: {e!r} ===\n")
        finally:
            CURRENT_PROC = None
    build.finished_at = time.time()
    dur = build.duration()
    if dur is not None:
        with open(build.log_path, "a") as lf:
            lf.write(f"=== duration: {dur:.0f}s ===\n")
    # Find the artifact the build produced (ipa preferred, else app, else archive).
    build.artifact = _find_artifact(Path(build.output_dir)) if build.output_dir else ""
    HISTORY.appendleft(build)
    save_history()
    prune_outputs(build.project_id)
    CURRENT = None


def _find_artifact(out_dir: Path) -> str:
    if not out_dir.exists():
        return ""
    for pattern in ("**/*.ipa", "**/*.xcarchive", "**/Release-iphoneos/*.app"):
        hits = sorted(out_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if hits:
            return str(hits[0])
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


async def worker_loop() -> None:
    while True:
        if not QUEUE:
            _wakeup.clear(); await _wakeup.wait(); continue
        build = QUEUE.popleft()
        project = reg.get(build.project_id)
        if not project:
            build.state = "failed"
            build.log_path.write_text("project no longer exists\n")
            HISTORY.appendleft(build); save_history(); continue
        await run_build(build, project)


@app.on_event("startup")
async def _startup() -> None:
    load_history()
    asyncio.create_task(worker_loop())


def enqueue(project: Project, source: str, deploy: bool) -> Build:
    b = Build(id=uuid.uuid4().hex[:12], project_id=project.id,
              project_name=project.name, source=source, deploy=deploy)
    QUEUE.append(b); _wakeup.set()
    return b


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
    return [p.public() for p in reg.list()]


@app.post("/api/projects/clone")
async def api_clone(request: Request, x_build_token: str = Header(default="")):
    _check_token(x_build_token)
    body = await request.json()
    name = (body.get("name") or "").strip()
    repo_url = (body.get("repo_url") or "").strip()
    branch = (body.get("branch") or "main").strip()
    if not name or not repo_url:
        raise HTTPException(status_code=400, detail="name and repo_url required")
    try:
        proj = onboarding.onboard_from_clone(reg, BUILDS_ROOT, name, repo_url, branch)
    except onboarding.OnboardError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return proj.public()


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
    try:
        proj = onboarding.onboard_from_path(reg, name, str(rp))
    except onboarding.OnboardError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return proj.public()


@app.patch("/api/projects/{project_id}")
async def api_edit(project_id: str, request: Request, x_build_token: str = Header(default="")):
    _check_token(x_build_token)
    body = await request.json()
    allowed = {"name", "branch", "development_team", "bundle_id", "device_udid",
               "webhook_deploys", "reclaim_disk", "clean_ignored", "pre_build_steps"}
    fields = {k: v for k, v in body.items() if k in allowed}
    proj = reg.update(project_id, **fields)
    if not proj:
        raise HTTPException(status_code=404, detail="no such project")
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
    if delete_files and proj.local_path:
        # Only delete clones we made under builds_root — never an adopted path elsewhere.
        p = Path(proj.local_path).resolve()
        if str(p).startswith(str(BUILDS_ROOT.resolve())) and p.exists():
            shutil.rmtree(p, ignore_errors=True)
    reg.remove(project_id)
    return {"removed": True}


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
async def trigger_build(project_id: str, x_build_token: str = Header(default=""),
                        deploy: Optional[bool] = Query(default=None)):
    _check_token(x_build_token)
    proj = reg.get(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="no such project")
    do_deploy = True if deploy is None else deploy
    return JSONResponse(enqueue(proj, "phone", do_deploy).public())


@app.post("/cancel/{build_id}")
async def cancel_build(build_id: str, x_build_token: str = Header(default="")):
    """Cancel a build: if it's running, kill its whole process group (Unity and
    xcodebuild included); if it's only queued, drop it from the queue."""
    _check_token(x_build_token)
    # Case 1: the running build.
    if CURRENT and CURRENT.id == build_id:
        CURRENT.state = "cancelled"          # run_build sees this and labels it
        proc = CURRENT_PROC
        if proc and proc.returncode is None:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)          # ask the whole group to stop
                # Escalate to SIGKILL shortly if it's still alive.
                async def _hard_kill(p, g):
                    await asyncio.sleep(8)
                    if p.returncode is None:
                        try: os.killpg(g, signal.SIGKILL)
                        except ProcessLookupError: pass
                asyncio.create_task(_hard_kill(proc, pgid))
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
            HISTORY.appendleft(b); save_history()
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

    proj = reg.get_by_repo(repo_url)
    if not proj:
        return JSONResponse({"skipped": True, "reason": f"no project matches {repo_url}"})
    if branch and proj.branch and branch != proj.branch:
        return JSONResponse({"skipped": True,
                             "reason": f"push to '{branch}', project builds '{proj.branch}'"})
    return JSONResponse(enqueue(proj, "gitea", proj.webhook_deploys).public())


@app.get("/status")
async def status():
    cur = None
    if CURRENT:
        cur = CURRENT.public()
        cur["avg_duration"] = avg_duration(CURRENT.project_id)   # for ETA/progress
    return {
        "current": cur,
        "queued": [b.public() for b in QUEUE],
        "history": [b.public() for b in list(HISTORY)[:10]],
    }


@app.get("/logs/{build_id}/raw", response_class=PlainTextResponse)
async def logs_raw(build_id: str):
    b = find_build(build_id)
    if not b or not b.log_path.exists():
        raise HTTPException(status_code=404, detail="no such build/log")
    return b.log_path.read_text(errors="replace")


@app.get("/logs/{build_id}")
async def logs_stream(build_id: str):
    b = find_build(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="no such build")

    async def gen():
        last = 0
        while True:
            p = b.log_path
            if p.exists():
                size = p.stat().st_size
                if size > last:
                    with open(p) as f:
                        f.seek(last); chunk = f.read(); last = size
                    for line in chunk.splitlines():
                        yield f"data: {line}\n\n"
            if b.state in ("success", "failed", "cancelled", "interrupted") and (
                not p.exists() or p.stat().st_size <= last):
                yield f"event: done\ndata: {b.state}\n\n"; break
            await asyncio.sleep(0.5)

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
async def install_artifact(project_id: str, build_id: str, x_build_token: str = Header(default="")):
    """Re-run a device install for an already-built artifact (phone reachable)."""
    _check_token(x_build_token)
    proj = reg.get(project_id)
    b = find_build(build_id)
    if not proj or not b or not b.artifact:
        raise HTTPException(status_code=404, detail="no such project/build/artifact")
    # Enqueue a lightweight install-only build; the script sees UBS_INSTALL_ONLY.
    nb = enqueue(proj, "install", True)
    nb.artifact = b.artifact
    return JSONResponse(nb.public())


@app.delete("/api/builds/{build_id}")
async def delete_build(build_id: str, x_build_token: str = Header(default="")):
    _check_token(x_build_token)
    b = find_build(build_id)
    if not b:
        raise HTTPException(status_code=404, detail="no such build")
    # Remove the output dir (only ever under OUTPUT_ROOT) and the log.
    if b.output_dir:
        p = Path(b.output_dir).resolve()
        if str(p).startswith(str(OUTPUT_ROOT.resolve())) and p.exists():
            shutil.rmtree(p, ignore_errors=True)
    if b.log_path.exists():
        b.log_path.unlink()
    try:
        HISTORY.remove(b)
    except ValueError:
        pass
    save_history()
    return {"removed": True}
