"""
Project registry — the service owns this. A JSON file on disk, one record per
Unity project, read and written by the server. No hand-edited env.sh anymore;
everything the build needs lives here and is managed through the web UI.
"""

import json
import os
import threading
import uuid
from dataclasses import dataclass, field, asdict, fields
from pathlib import Path
from typing import Optional


@dataclass
class PreBuildStep:
    """One optional, project-defined step run before the Unity player build.

    type:
      "executeMethod" -> runs `<unity> -batchmode -executeMethod <value>` in the
                         project (a separate Unity pass). value is the fully
                         qualified static C# method, e.g.
                         VoxelMage.EditorTools.DefinitionBlobBuilderWindow.BuildHeadless
      "shell"         -> runs `value` as a shell command in the project dir, with
                         the same UBS_* env the build has. For CCD CLI uploads etc.
    The service knows nothing about blobs or CCD; these are just steps you list.
    Steps run in order; the first failure aborts the whole build.
    """
    name: str = ""
    type: str = "executeMethod"         # "executeMethod" | "shell"
    value: str = ""
    enabled: bool = True


@dataclass
class Project:
    id: str
    name: str
    repo_url: str                       # e.g. http://terronas:3000/etc/game.git
    branch: str = "main"
    local_path: str = ""                # where it's cloned on the mini
    unity_version: str = ""             # auto-detected from ProjectVersion.txt
    # Apple bits — the only things a human provides, once, via the form:
    development_team: str = ""          # 10-char Apple Team ID
    bundle_id: str = ""
    device_udid: str = ""               # target iPhone, optional
    # behaviour
    webhook_deploys: bool = False       # does a git push also install to phone?
    reclaim_disk: bool = True           # uninstall other editor versions when installing
    clean_ignored: bool = False         # git clean -fdx (nukes Library/) if true
    build_script_installed: bool = False  # has BuildScript.cs been dropped in?
    # Optional ordered pre-build steps (blob build, CCD upload, …) — see above.
    pre_build_steps: list = field(default_factory=list)

    def __post_init__(self):
        # Normalize steps to dicts (they arrive as dicts from JSON or the API).
        norm = []
        for s in self.pre_build_steps or []:
            if isinstance(s, PreBuildStep):
                norm.append(asdict(s))
            elif isinstance(s, dict):
                norm.append({
                    "name": s.get("name", ""),
                    "type": s.get("type", "executeMethod"),
                    "value": s.get("value", ""),
                    "enabled": bool(s.get("enabled", True)),
                })
        self.pre_build_steps = norm

    def public(self) -> dict:
        return asdict(self)

    @classmethod
    def from_record(cls, rec: dict) -> "Project":
        """Build from a stored record, ignoring unknown keys and filling defaults
        for keys added in later versions (so old projects.json files still load)."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in rec.items() if k in known})


class Registry:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})

    def _read(self) -> dict:
        with open(self.path) as f:
            return json.load(f)

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def list(self) -> list[Project]:
        return [Project.from_record(r) for r in self._read().values()]

    def get(self, project_id: str) -> Optional[Project]:
        r = self._read().get(project_id)
        return Project.from_record(r) if r else None

    def get_by_repo(self, repo_url: str) -> Optional[Project]:
        """Match a webhook's repo URL to a project. Normalizes trailing .git and slashes."""
        def norm(u: str) -> str:
            return u.rstrip("/").removesuffix(".git").lower()
        target = norm(repo_url)
        for r in self._read().values():
            if norm(r["repo_url"]) == target:
                return Project.from_record(r)
        return None

    def add(self, project: Project) -> Project:
        with self._lock:
            data = self._read()
            data[project.id] = project.public()
            self._write(data)
        return project

    def update(self, project_id: str, **fields) -> Optional[Project]:
        with self._lock:
            data = self._read()
            if project_id not in data:
                return None
            data[project_id].update(fields)
            self._write(data)
            return Project.from_record(data[project_id])

    def remove(self, project_id: str) -> bool:
        with self._lock:
            data = self._read()
            if project_id not in data:
                return False
            del data[project_id]
            self._write(data)
            return True

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]
