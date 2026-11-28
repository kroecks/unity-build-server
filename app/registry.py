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
    device_udid: str = ""               # LEGACY single target (iOS UDID / adb serial
                                        # / host:port). Still honoured when
                                        # deploy_devices is empty; the UI now edits
                                        # deploy_devices instead.
    # Device targeting (ids from the device inventory, app/devices.py):
    # target_kinds limits which kinds of device this project may deploy to —
    # e.g. ["quest"] so a Quest build never lands on an Android phone. Empty =
    # every kind of the project's platform (ios: iphone+ipad; android: quest+android).
    target_kinds: list = field(default_factory=list)
    # Default deploy targets for Build / webhook deploys (several = install to
    # each one that's online). Install and "Build to…" can pick others per run.
    deploy_devices: list = field(default_factory=list)
    # Android-only. Keystore path + alias live here; the PASSWORDS do not — they
    # come from host config (config.json "android_keystores"), like the ASC key.
    # Blank keystore_path = Unity's debug keystore (fine for sideloading/Quest dev).
    android_build_aab: bool = False     # True = .aab (Play Store), False = .apk (adb / Quest)
    keystore_path: str = ""             # absolute path on the mini, e.g. ~/keys/game.keystore
    keystore_alias: str = ""
    # Version-code conflicts with what's already on a device (Android refuses
    # INSTALL_FAILED_VERSION_DOWNGRADE when the installed versionCode is higher):
    # bump_version_from_devices: before a deploying build, read the installed
    #   versionCode from each target and raise next_build_number past it, so the
    #   build can always install. No data loss; the number just jumps ahead.
    # uninstall_on_conflict: if an install STILL conflicts (an older artifact
    #   re-pushed with Install, a newer build landed mid-build, or a different
    #   signing key), uninstall the app and reinstall. WIPES the app's data.
    bump_version_from_devices: bool = True
    uninstall_on_conflict: bool = False
    # LEGACY: the per-project keep-awake switch. Keep-awake is now part of the
    # launch config (a launch profile / launch plan entry); these are folded in
    # at startup by LaunchProfiles.migrate_from and then switched off.
    keep_awake_after_deploy: bool = False
    keep_awake_minutes: int = 10
    # Android launch arguments. After an install the app is started with
    # `am start -e unity "<args>"`, which Unity hands to the player as its
    # command line. The saved argument sets ("launch profiles") are SHARED by
    # every project (LaunchProfiles below); a project only picks its default:
    # the profile name used by Build, git-push deploys and preselected in the
    # Install dialog ("" = launch with no args).
    default_launch_profile: str = ""
    # Per-target overrides of that default, {device id: profile name} ("" =
    # launch that target plainly). Keyed by target id so other target kinds
    # (simulators, local processes) can slot in later.
    default_launches: dict = field(default_factory=dict)
    # LEGACY: per-project profiles from before they were shared. Moved into the
    # shared list at startup (LaunchProfiles.migrate_from) and then cleared.
    launch_profiles: list = field(default_factory=list)
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
    # Onboarding lifecycle. Clone-onboarding writes the record BEFORE the
    # (possibly hours-long) download starts, so a page refresh or a server
    # restart never loses it:
    #   ""        -> ready (the default, so existing records are unchanged)
    #   "cloning" -> git download / checkout / verify in progress; resumed
    #                automatically when the server starts
    #   "failed"  -> stopped with onboard_error; Retry resumes from what's on disk
    onboard_state: str = ""
    onboard_error: str = ""
    onboard_started_at: float = 0.0

    @property
    def ready(self) -> bool:
        return not self.onboard_state

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
        self.launch_profiles = [
            {"name": str(lp.get("name", "")).strip(), "args": str(lp.get("args", ""))}
            for lp in (self.launch_profiles or []) if isinstance(lp, dict)
        ]


    def public(self) -> dict:
        return asdict(self)

    @classmethod
    def from_record(cls, rec: dict) -> "Project":
        """Build from a stored record, ignoring unknown keys and filling defaults
        for keys added in later versions (so old projects.json files still load)."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in rec.items() if k in known})


def clean_profile(x: dict) -> dict:
    """A launch profile: how to START the app on a target — not how it's built.
      name                the label projects and launch plans refer to
      args                command-line args (Android: `am start -e unity "<args>"`)
      keep_awake_minutes  0 = off; else keep the device awake that long after
                          launch (Quest: proximity override; varies by device)
    New launch settings belong here, so every target kind (devices today,
    simulators / local processes later) gets them the same way."""
    try:
        mins = max(0, int(x.get("keep_awake_minutes") or 0))
    except (TypeError, ValueError):
        mins = 0
    return {"name": str(x.get("name", "")), "args": str(x.get("args", "")),
            "keep_awake_minutes": mins}


class LaunchProfiles:
    """The shared launch-profile library (see clean_profile), one JSON file next
    to projects.json. Every project picks its default from this list."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def all(self) -> list:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        return [clean_profile(x) for x in data if isinstance(x, dict)] if isinstance(data, list) else []

    def get(self, name: str) -> Optional[dict]:
        return next((x for x in self.all() if x["name"] == name), None)

    def replace(self, profiles: list) -> list:
        with self._lock:
            self._write(profiles)
        return profiles

    def _write(self, profiles: list) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(profiles, f, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise RegistryWriteError(f"couldn't save launch profiles to {self.path}: {e}") from e

    def migrate_from(self, registry: "Registry") -> list:
        """Move any per-project profiles (older records) into the shared list.
        Same name + same args = one profile; same name + different args keeps
        both, the second renamed "<name> (<project>)" (and that project's
        default follows the rename). Returns notes for the server log."""
        notes = []
        with self._lock:
            shared = self.all()
            changed = False
            for proj in registry.list():
                if not proj.launch_profiles:
                    continue
                dflt = proj.default_launch_profile
                for lp in proj.launch_profiles:
                    name = lp["name"]
                    if not name:
                        continue
                    have = next((x for x in shared if x["name"] == name), None)
                    if have and have["args"] == lp["args"]:
                        continue
                    if have:
                        new = f"{name} ({proj.name})"
                        notes.append(f"launch profile '{name}' of {proj.name} differs from the shared one; kept as '{new}'")
                        if dflt == name:
                            dflt = new
                        name = new
                    shared.append(clean_profile({"name": name, "args": lp["args"]}))
                    changed = True
                if changed:
                    self._write(shared)
                registry.update(proj.id, launch_profiles=[], default_launch_profile=dflt)
                notes.append(f"moved {proj.name}'s launch profiles into the shared list")
            # Per-project keep-awake -> launch config. The project's default
            # profile gets the keep-awake time (if it had none); with no default
            # profile, a shared "Keep awake" profile (no args) becomes its default.
            for proj in registry.list():
                if not proj.keep_awake_after_deploy:
                    continue
                mins = max(1, int(proj.keep_awake_minutes or 10))
                dflt = proj.default_launch_profile
                prof = next((x for x in shared if x["name"] == dflt), None) if dflt else None
                if prof is None:
                    dflt = "Keep awake"
                    prof = next((x for x in shared if x["name"] == dflt), None)
                    if prof is None:
                        prof = clean_profile({"name": dflt, "args": ""})
                        shared.append(prof)
                if not prof["keep_awake_minutes"]:
                    prof["keep_awake_minutes"] = mins
                self._write(shared)
                registry.update(proj.id, keep_awake_after_deploy=False, default_launch_profile=dflt)
                notes.append(f"{proj.name}: keep-awake is now part of launch profile '{dflt}' "
                             f"({prof['keep_awake_minutes']} min)")
        return notes


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

    def claim_build_number(self, project_id: str, at_least: int = 0) -> int:
        """Atomically return this project's next build number and increment the
        stored counter, so each build gets a unique, monotonically-increasing
        number (stores reject duplicates). Under the lock so two builds can't
        claim the same one. `at_least` raises the floor (e.g. past a versionCode
        already installed on a device); the counter never goes backwards."""
        with self._lock:
            data = self._read()
            if project_id not in data:
                return 0
            n = int(data[project_id].get("next_build_number", 1) or 1)
            n = max(n, int(at_least or 0))
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