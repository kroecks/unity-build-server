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


class RegistryWriteError(Exception):
    """Raised when the registry can't be persisted (e.g. disk full)."""


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
    # Build target. "ios" (default, so existing records are unchanged) or
    # "android" (also Quest — an Android APK with Meta's runtime). One project
    # per platform: an android project is a separate record on the same repo,
    # building the `android` branch with merge_from: main.
    platform: str = "ios"               # "ios" | "android"
    # Apple bits — the only things a human provides, once, via the form:
    development_team: str = ""          # 10-char Apple Team ID (iOS only)
    bundle_id: str = ""                 # iOS bundle id / Android package name
    device_udid: str = ""               # iOS: target iPhone UDID. Android: adb
                                        # serial (`adb devices`), or host:port for
                                        # ADB-over-WiFi. Optional; blank = build only.
    # Android-only. Keystore path + alias live here; the PASSWORDS do not — they
    # come from host config (config.json "android_keystores"), like the ASC key.
    # Blank keystore_path = Unity's debug keystore (fine for sideloading/Quest dev).
    android_build_aab: bool = False     # True = .aab (Play Store), False = .apk (adb / Quest)
    keystore_path: str = ""             # absolute path on the mini, e.g. ~/keys/game.keystore
    keystore_alias: str = ""
    # behaviour
    webhook_deploys: bool = False       # does a git push also install to phone?
    reclaim_disk: bool = True           # uninstall other editor versions when installing
    clean_ignored: bool = False         # git clean -fdx (nukes Library/) if true
    build_script_installed: bool = False  # has BuildScript.cs been dropped in?
    # Per-project CCD / content settings. The service account auth is shared
    # (host env: UGS_CLI_SERVICE_KEY_ID/SECRET); these differ per project and are
    # exposed to pre-build steps as $UBS_CCD_* so one shared step scales to all.
    ccd_project_id: str = ""            # Unity project id (per game)
    ccd_environment: str = ""           # environment name, e.g. "production"
    ccd_bucket: str = ""                # CCD bucket name
    addressables_profile: str = ""      # Addressables profile to build (e.g. "Development")
    remote_build_path: str = ""         # where Addressables writes bundles (e.g. "ServerData/iOS")
    scripting_defines: str = ""         # extra scripting define symbols for the build,
                                        # e.g. "TK_IOS_DEV" — needed so the CCD auth
                                        # header code is compiled in (else content = 401)
    merge_from: str = ""                # before building <branch>, merge this branch
                                        # into it (e.g. "main" when branch is "ios").
                                        # CLEAN/fast-forward only; a conflict ABORTS the
                                        # build and leaves the branch untouched. Empty = off.
    auto_distribute: bool = False       # upload successful webhook builds to TestFlight
                                        # automatically (uses shared ASC key in config)
    next_build_number: int = 1          # monotonic build number the server injects +
                                        # increments each build, so stores never reject
                                        # a duplicate (iOS buildNumber / Android bundleVersionCode)
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
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            # Clean up the partial temp file so it doesn't linger, and raise a
            # clear error the API layer turns into a readable message instead of
            # a raw 500 traceback. The real projects.json is untouched (we only
            # os.replace after a fully-written temp), so nothing is corrupted.
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            import errno
            if e.errno == errno.ENOSPC:
                raise RegistryWriteError(
                    "No space left on the build machine's disk — the change "
                    "could not be saved. Free up space (old build outputs under "
                    "output_root are the usual cause) and try again."
                ) from e
            raise RegistryWriteError(f"Could not save registry: {e}") from e

    def list(self) -> list[Project]:
        return [Project.from_record(r) for r in self._read().values()]

    def get(self, project_id: str) -> Optional[Project]:
        r = self._read().get(project_id)
        return Project.from_record(r) if r else None

    @staticmethod
    def _norm_url(u: str) -> str:
        return (u or "").rstrip("/").removesuffix(".git").lower()

    def get_by_repo(self, repo_url: str) -> Optional[Project]:
        """First project matching a repo URL. Normalizes trailing .git and slashes.
        Prefer list_by_repo for webhooks: one repo can now back several projects
        (e.g. an ios project and an android project)."""
        hits = self.list_by_repo(repo_url)
        return hits[0] if hits else None

    def list_by_repo(self, repo_url: str) -> "list[Project]":
        """Every project whose repo URL matches (ios + android on the same repo)."""
        target = self._norm_url(repo_url)
        return [Project.from_record(r) for r in self._read().values()
                if self._norm_url(r.get("repo_url", "")) == target]

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

    def claim_build_number(self, project_id: str) -> int:
        """Atomically return this project's next build number and increment the
        stored counter, so each build gets a unique, monotonically-increasing
        number (stores reject duplicates). Under the lock so two builds can't
        claim the same one."""
        with self._lock:
            data = self._read()
            if project_id not in data:
                return 0
            n = int(data[project_id].get("next_build_number", 1) or 1)
            data[project_id]["next_build_number"] = n + 1
            self._write(data)
            return n

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