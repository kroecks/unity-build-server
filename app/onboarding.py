"""
Onboarding + verification logic the service runs so you never hand-edit a script.

Two ways to onboard a project:
  1. clone from Gitea into the managed builds dir. The server registers the
     project first (onboard_state="cloning") and runs
     finish_clone_onboarding() in the background, so a refresh or restart
     mid-download never loses it; the download resumes from what's on disk.
  2. onboard_from_path(local_path) -> adopt a project already cloned on the
     mini (used by the server-side directory browser)

Both then run verify_project(), which:
  - detects the Unity version from ProjectSettings/ProjectVersion.txt
  - reads the git remote/branch
  - installs BuildScript.cs into Assets/Editor/ if missing
  - reports what's present so the UI can show a checklist
"""

import asyncio
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from .registry import Project, Registry

# The C# build entry point lives in scripts/BuildScript.cs as the single source
# of truth; the build script reinstalls it after every git clean. We read it
# from there so onboarding and the build never drift apart.
_CS_SOURCE = Path(__file__).parent.parent / "scripts" / "BuildScript.cs"


def _build_script_cs() -> str:
    try:
        return _CS_SOURCE.read_text()
    except OSError:
        # Fallback keeps onboarding working even if the file is missing; the
        # build step installs the canonical copy anyway.
        return ("#if UNITY_EDITOR\n// placeholder; build.sh installs the real "
                "BuildScript.cs from scripts/BuildScript.cs\n#endif\n")


class OnboardError(Exception):
    pass


def detect_unity_version(project_path: Path) -> str:
    pv = project_path / "ProjectSettings" / "ProjectVersion.txt"
    if not pv.exists():
        raise OnboardError(
            f"Not a Unity project: {pv} not found. "
            "Point at the folder that contains Assets/ and ProjectSettings/."
        )
    text = pv.read_text()
    m = re.search(r"m_EditorVersion:\s*(\S+)", text)
    if not m:
        raise OnboardError("Could not parse Unity version from ProjectVersion.txt")
    return m.group(1).strip()


def git_remote_and_branch(project_path: Path) -> tuple[str, str]:
    def run(*args) -> str:
        return subprocess.run(
            ["git", "-C", str(project_path), *args],
            capture_output=True, text=True, check=False
        ).stdout.strip()
    remote = run("config", "--get", "remote.origin.url")
    branch = run("rev-parse", "--abbrev-ref", "HEAD") or "main"
    return remote, branch


def install_build_script(project_path: Path) -> bool:
    editor_dir = project_path / "Assets" / "Editor"
    editor_dir.mkdir(parents=True, exist_ok=True)
    target = editor_dir / "BuildScript.cs"
    cs = _build_script_cs()
    # Only (re)write if missing or changed, so we don't dirty the tree needlessly.
    if target.exists() and target.read_text() == cs:
        return True
    target.write_text(cs)
    return True


def _norm_url(u: str) -> str:
    return (u or "").strip().rstrip("/").removesuffix(".git").lower()


def clone_dest(builds_root: Path, name: str) -> Path:
    return Path(builds_root) / _safe_dirname(name)


def check_clone_dest(dest: Path, repo_url: str) -> bool:
    """Make sure dest can receive a clone of repo_url. Returns True if dest
    already holds (part of) a clone of that same repo — e.g. one left behind by
    an interrupted onboarding — which the clone then resumes instead of
    re-downloading. Raises OnboardError for anything else that's in the way."""
    if not dest.exists() or not any(dest.iterdir()):
        return False
    if (dest / ".git").is_dir():
        origin = subprocess.run(
            ["git", "-C", str(dest), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, check=False).stdout.strip()
        if not origin or _norm_url(origin) == _norm_url(repo_url):
            return True
        raise OnboardError(
            f"{dest} already holds a clone of a different repo ({origin}). "
            "Pick another project name, or delete that folder.")
    raise OnboardError(f"Destination {dest} already exists and is not empty.")


def _git_env() -> dict:
    env = os.environ.copy()
    # Never wait on an interactive prompt nobody can see: a missing credential
    # or an unknown SSH host key fails fast with a readable error instead of
    # hanging the onboarding forever.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


async def _git(log, *args: str, cwd: Optional[Path] = None) -> int:
    """Run git, streaming its output into the open onboarding log. If the
    surrounding task is cancelled (project removed), git is killed too."""
    log.write(f"$ git {' '.join(args)}\n"); log.flush()
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd) if cwd else None,
        stdout=log, stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL, env=_git_env(),
        start_new_session=True)   # own process group, so cancel reaches its helpers
    try:
        return await proc.wait()
    except asyncio.CancelledError:
        # git fetch runs helpers (git-remote-https, ssh, index-pack); kill the
        # whole group, not just the top-level git, or they keep downloading.
        if proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        raise


async def _git_out(*args: str, cwd: Path) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=_git_env())
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace").strip()


async def clone_resumable(dest: Path, repo_url: str, branch: str, log_path: Path) -> None:
    """Get repo_url@branch into dest, picking up whatever is already there.

    Instead of one all-or-nothing `git clone`, this is init → fetch → checkout.
    Each step is safe to re-run, so a clone interrupted by a server restart (or
    a failure) is finished by simply calling this again: a repo already fully
    on disk only costs a quick fetch, and git keeps any objects it had."""
    dest.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as log:
        log.write(f"\n=== {time.ctime()} | {repo_url} @ {branch} -> {dest} ===\n")
        log.flush()
        if not (dest / ".git").is_dir():
            if await _git(log, "init", "-q", str(dest)) != 0:
                raise OnboardError("git init failed (see the onboarding log)")
        rc, origin = await _git_out("config", "--get", "remote.origin.url", cwd=dest)
        if rc != 0 or not origin:
            if await _git(log, "remote", "add", "origin", repo_url, cwd=dest) != 0:
                raise OnboardError("git remote add failed (see the onboarding log)")
        # Same refs `git clone` would create (refs/remotes/origin/*), which is
        # what build.sh's fetch + reset --hard origin/<branch> expects.
        if await _git(log, "fetch", "--progress", "--prune", "origin", cwd=dest) != 0:
            raise OnboardError(_last_error(log_path) or "git fetch failed")
        rc, _ = await _git_out("rev-parse", "--verify", "-q",
                               f"refs/remotes/origin/{branch}", cwd=dest)
        if rc != 0:
            _, heads = await _git_out("for-each-ref", "--format=%(refname:lstrip=3)",
                                      "refs/remotes/origin", cwd=dest)
            have = ", ".join(h for h in heads.splitlines() if h != "HEAD") or "none"
            raise OnboardError(f"Branch '{branch}' not found on the remote "
                               f"(branches: {have}).")
        # -f: a checkout cut off half-way leaves files git now sees as
        # untracked; overwrite them rather than refusing.
        if await _git(log, "checkout", "-f", "-B", branch, "--track",
                      f"origin/{branch}", cwd=dest) != 0:
            raise OnboardError(_last_error(log_path) or "git checkout failed")
        log.write("=== download complete ===\n")


def read_progress(log_path: Path, max_bytes: int = 16384) -> str:
    """Latest progress line from an onboarding log (git rewrites progress in
    place with \\r, so the last \\r/\\n-separated fragment is the current one)."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            tail = f.read().decode(errors="replace")
    except OSError:
        return ""
    parts = [p.strip() for p in re.split(r"[\r\n]+", tail) if p.strip()]
    return parts[-1][:300] if parts else ""


def _last_error(log_path: Path) -> str:
    """The fatal/error lines git printed last, for a readable failure message."""
    try:
        text = Path(log_path).read_text(errors="replace")[-8192:]
    except OSError:
        return ""
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", text)]
    errs = [ln for ln in lines if ln.lower().startswith(("fatal:", "error:", "remote: error"))]
    return "\n".join(errs[-3:])


def verify_project(project_path: Path) -> dict:
    """Inspect a project folder and return everything the registry needs."""
    project_path = Path(project_path)
    if not project_path.exists():
        raise OnboardError(f"Path does not exist: {project_path}")
    version = detect_unity_version(project_path)
    remote, branch = git_remote_and_branch(project_path)
    installed = install_build_script(project_path)
    return {
        "unity_version": version,
        "repo_url": remote,
        "branch": branch,
        "build_script_installed": installed,
    }


async def finish_clone_onboarding(reg: Registry, project_id: str, log_path: Path) -> None:
    """Background job behind clone-onboarding: download (resuming whatever is
    on disk), verify, and flip the record to ready. Any failure is stored on
    the record as onboard_state="failed" so the UI can show it and offer Retry."""
    proj = reg.get(project_id)
    if not proj:
        return
    try:
        await clone_resumable(Path(proj.local_path), proj.repo_url, proj.branch, log_path)
        info = await asyncio.to_thread(verify_project, Path(proj.local_path))
    except asyncio.CancelledError:
        raise
    except OnboardError as e:
        _log_line(log_path, f"=== failed: {e} ===")
        reg.update(project_id, onboard_state="failed", onboard_error=str(e))
        return
    except Exception as e:  # never leave a record stuck in "cloning"
        _log_line(log_path, f"=== failed: {e!r} ===")
        reg.update(project_id, onboard_state="failed", onboard_error=repr(e))
        return
    _log_line(log_path, f"=== onboarded: Unity {info['unity_version']} ===")
    reg.update(project_id, onboard_state="", onboard_error="",
               unity_version=info["unity_version"],
               build_script_installed=info["build_script_installed"])


def _log_line(log_path: Path, text: str) -> None:
    try:
        with open(log_path, "a") as f:
            f.write(text + "\n")
    except OSError:
        pass


def onboard_from_path(reg: Registry, name: str, local_path: str) -> Project:
    info = verify_project(Path(local_path))
    proj = Project(
        id=Registry.new_id(), name=name,
        repo_url=info["repo_url"], branch=info["branch"],
        local_path=str(local_path),
        unity_version=info["unity_version"],
        build_script_installed=info["build_script_installed"],
    )
    return reg.add(proj)


def _safe_dirname(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("_") or "project"
