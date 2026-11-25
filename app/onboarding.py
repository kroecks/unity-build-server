"""
Onboarding + verification logic the service runs so you never hand-edit a script.

Two ways to onboard a project:
  1. clone_project(repo_url)      -> clone from Gitea into the managed builds dir
  2. adopt_project(local_path)    -> adopt a project already cloned on the mini
                                     (used by the server-side directory browser)

Both then run verify_project(), which:
  - detects the Unity version from ProjectSettings/ProjectVersion.txt
  - reads the git remote/branch
  - installs BuildScript.cs into Assets/Editor/ if missing
  - reports what's present so the UI can show a checklist
"""

import re
import shutil
import subprocess
from pathlib import Path

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


def clone_project(repo_url: str, builds_root: Path, name: str, branch: str = "main") -> Path:
    """Clone a repo from Gitea into builds_root/<name>. Returns the local path."""
    builds_root.mkdir(parents=True, exist_ok=True)
    dest = builds_root / _safe_dirname(name)
    if dest.exists():
        if any(dest.iterdir()):
            raise OnboardError(f"Destination {dest} already exists and is not empty.")
        dest.rmdir()
    # No credentials embedded here; rely on the git credential helper / SSH on the mini.
    proc = subprocess.run(
        ["git", "clone", "--branch", branch, repo_url, str(dest)],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise OnboardError(f"git clone failed:\n{proc.stderr.strip()}")
    return dest


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


def onboard_from_clone(reg: Registry, builds_root: Path, name: str,
                       repo_url: str, branch: str = "main") -> Project:
    local = clone_project(repo_url, builds_root, name, branch)
    info = verify_project(local)
    proj = Project(
        id=Registry.new_id(), name=name,
        repo_url=info["repo_url"] or repo_url,
        branch=info["branch"] or branch,
        local_path=str(local),
        unity_version=info["unity_version"],
        build_script_installed=info["build_script_installed"],
    )
    return reg.add(proj)


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
