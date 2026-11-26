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

from .registry import Registry, Project, RegistryWriteError
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

# App Store Connect API key for TestFlight uploads (shared across projects).
# MUST be a TEAM key (ASC → Users and Access → Integrations → Team Keys), not an
# Individual key — altool/xcodebuild reject individual keys with "-26000 Failure
# to authenticate". Point asc_key_path at the AuthKey_<KEYID>.p8 file.
ASC_KEY_ID = CFG.get("asc_key_id", "")
ASC_ISSUER_ID = CFG.get("asc_issuer_id", "")
ASC_KEY_PATH = str(Path(CFG["asc_key_path"]).expanduser()) if CFG.get("asc_key_path") else ""

reg = Registry(REGISTRY_PATH)
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
HISTORY: "deque[Build]" = deque(maxlen=500)   # keep plenty; UI pages through them
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
        # Per-project CCD/content values, for pre-build steps to reference.
        # The ugs service-account auth itself stays in the shared host env.
        "UBS_CCD_PROJECT_ID": project.ccd_project_id,
        "UBS_CCD_ENVIRONMENT": project.ccd_environment,
        "UBS_CCD_BUCKET": project.ccd_bucket,
        "UBS_ADDRESSABLES_PROFILE": project.addressables_profile,
        "UBS_REMOTE_BUILD_PATH": project.remote_build_path,
        "UBS_SCRIPTING_DEFINES": project.scripting_defines,
        "UBS_MERGE_FROM": project.merge_from,
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
    global CURRENT_PROC
    with open(build.log_path, "w") as logf:
        logf.write(f"=== build {build.id} | {project.name} | {time.ctime()} ===\n")
        logf.flush()
        try:
            # Launch in its own process group so cancel can kill Unity/xcodebuild
            # too, not just the top-level bash (which would orphan them and leave
            # the project lock held).
            # NOTE: raise the stream buffer limit AND read in fixed-size chunks
            # (not line-by-line). IL2CPP/Xcode emit single lines far longer than
            # asyncio's default 64 KB readline limit; `async for line in stdout`
            # throws "Separator is not found, and chunk exceeded the limit" on
            # those and kills the reader mid-build. Chunked reads can't overflow.
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash", BUILD_SCRIPT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=env,
                start_new_session=True,   # setsid: new process group
                limit=1024 * 1024,        # 1 MB stream buffer (was 64 KB default)
            )
            CURRENT_PROC = proc
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                logf.write(chunk.decode(errors="replace")); logf.flush()
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
    # Auto-distribute: a successful webhook build of a project that opted in gets
    # queued for TestFlight upload (only if ASC is configured). Never auto-distribute
    # install/distribute runs themselves, or manual/failed builds.
    if (build.state == "success" and build.source == "gitea"
            and project.auto_distribute and ASC_KEY_ID and ASC_ISSUER_ID and ASC_KEY_PATH):
        archive = _find_archive(build)
        if archive:
            nb = enqueue(project, "distribute", False)
            if nb:
                nb.artifact = archive


def _find_artifact(out_dir: Path) -> str:
    if not out_dir.exists():
        return ""
    for pattern in ("**/*.ipa", "**/*.xcarchive", "**/Release-iphoneos/*.app"):
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


def enqueue(project: Project, source: str, deploy: bool, coalesce: bool = False):
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
               "webhook_deploys", "reclaim_disk", "clean_ignored", "pre_build_steps",
               "ccd_project_id", "ccd_environment", "ccd_bucket",
               "addressables_profile", "remote_build_path", "scripting_defines",
               "merge_from", "auto_distribute"}
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
        return JSONResponse({"skipped": True,
                             "reason": f"push to '{branch}', this project builds on "
                                       + ("/".join(sorted(triggers)))})
    # Coalesce rapid pushes: don't stack redundant builds for the same project.
    b = enqueue(proj, "gitea", proj.webhook_deploys, coalesce=True)
    if b is None:
        return JSONResponse({"coalesced": True,
                             "reason": "a build for this project is already queued; "
                                       "it will pick up the latest commits"})
    return JSONResponse(b.public())


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


@app.get("/device-logs/{project_id}")
async def device_logs(project_id: str):
    """Stream the device console (xcrun devicectl) for a project's target device,
    so the app's runtime output (addressables errors, exceptions) is visible in
    the UI. Filtered to the app's process when possible. SSE, like build logs."""
    proj = reg.get(project_id)
    if not proj or not proj.device_udid:
        raise HTTPException(status_code=404, detail="no project or no device UDID set")

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
               "--console", "--device", proj.device_udid, proj.bundle_id]
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
async def device_logs_zip(project_id: str, minutes: int = 30):
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
    if not proj or not proj.device_udid:
        raise HTTPException(status_code=404, detail="no project or no device UDID set")
    udid = proj.device_udid
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
async def device_logs_last(project_id: str, minutes: int = 10):
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
    if not proj or not proj.device_udid:
        raise HTTPException(status_code=404, detail="no project or no device UDID set")

    udid = proj.device_udid
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


@app.post("/build/{project_id}/distribute/{build_id}")
async def distribute_build(project_id: str, build_id: str, x_build_token: str = Header(default="")):
    """Export a built archive for App Store and upload it to TestFlight."""
    _check_token(x_build_token)
    proj = reg.get(project_id)
    b = find_build(build_id)
    if not proj or not b:
        raise HTTPException(status_code=404, detail="no such project/build")
    if not (ASC_KEY_ID and ASC_ISSUER_ID and ASC_KEY_PATH):
        raise HTTPException(status_code=400, detail="App Store Connect key not configured "
                            "(set asc_key_id / asc_issuer_id / asc_key_path in config.json)")
    archive = _find_archive(b)
    if not archive:
        raise HTTPException(status_code=404, detail="no .xcarchive for this build to distribute")
    nb = enqueue(proj, "distribute", False)
    nb.artifact = archive   # the build script reads this as UBS_DISTRIBUTE_ARCHIVE
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
async def list_builds(offset: int = 0, limit: int = 20, project_id: str = ""):
    """Paginated full build history (newest first). The UI pages through this;
    /status still carries just the few most-recent for the live top view."""
    items = [b for b in HISTORY if not project_id or b.project_id == project_id]
    total = len(items)
    page = items[offset: offset + max(1, min(limit, 200))]
    return {"total": total, "offset": offset, "limit": limit,
            "builds": [b.public() for b in page]}


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