# Wiring the blob + CCD pre-build steps (voxelmage)

This is the project-specific glue that makes the app launch with its remote
content. Without it, the build compiles but the app points at CCD content that
was never built or uploaded — the "stuck at load / wrong addressables" symptom.

## The ordering that must hold

    blob build  ->  Addressables build (blob gets packaged in)  ->  CCD upload

The first two are Unity operations (one `executeMethod` launch). The third is a
shell step using the `ugs` CLI, because the CCD Management SDK does NOT work in
batchmode (Unity confirms it's editor-only — it can't resolve the cloud project
id without the GUI). So we build in Unity, upload with the CLI.

> NOTE: the CLI is `ugs` (Unity Gaming Services). The old `ucd` CLI is
> DEPRECATED. CCD commands run as `ugs ccd ...`.

## Step A — add the C# entry point to your project

1. Copy `CIBuild.cs` into your project at `Assets/Editor/CIBuild.cs`.
2. Set `PROFILE_NAME` to the Addressables profile your CCD bucket is wired to
   (the one you pick by hand in the Groups window today). Default `"Development"`.
3. Commit it to the branch the build server builds (e.g. your `ios` branch) —
   the server does not auto-install this one, so it must be in the repo.

## Step B — install + authenticate the `ugs` CLI on the Mac mini (one time)

1. Install the UGS CLI (see https://docs.unity.com/en-us/ccd/cli), then:

       ugs --help        # confirm it runs

2. Create a **service account** key in the Unity Dashboard:
   Manage organization -> Service accounts -> Add key. Grant it:
   - **LiveOps > Cloud Content Delivery API Public Editor** (org role)
   - **Admin > Unity Environments Viewer** (project role)

3. Authenticate. Best for a build box: set env vars (put these in the launchd
   plist alongside your Unity service account vars, so unattended runs work):

       export UGS_CLI_SERVICE_KEY_ID="your_key_id"
       export UGS_CLI_SERVICE_SECRET_KEY="your_secret_key"

   Or log in interactively once: `ugs login` (stores the key locally).
   Check with `ugs login status`.

4. Point it at your project/env/bucket (find these IDs in the CCD dashboard):

       ugs env use <environment-name>
       ugs config set project-id <project-id>
       ugs config set bucket-name <bucket-name>

   (Confirm `config set bucket-name` vs `bucket` with `ugs config --help` —
   the docs show both depending on version.)

## Step C — fill the per-project Content/CCD fields in the UI

Open the project -> Edit -> Content / CCD, and fill:
- CCD project id        (this game's Unity project id)
- Environment           (e.g. production)
- CCD bucket            (the bucket name)
- Addressables profile  (the profile wired to that bucket, e.g. Development)
- Remote build path     (where Addressables writes bundles, e.g. ServerData/iOS)

These are exposed to the pre-build steps as $UBS_CCD_PROJECT_ID,
$UBS_CCD_ENVIRONMENT, $UBS_CCD_BUCKET, $UBS_ADDRESSABLES_PROFILE, and
$UBS_REMOTE_BUILD_PATH. Because of this, the two pre-build steps below are
IDENTICAL for every project — only these field values differ.

## Step D — configure the two pre-build steps in the UI

Open the project -> Edit -> Pre-build steps. Add these THREE, IN ORDER.
They are the same for every project:

### Step 0 — set scripting defines (type: executeMethod)
- name:  `set defines`
- type:  `executeMethod`
- value: `VoxelMage.EditorTools.CIBuild.SetScriptingDefines`

This applies the "Scripting defines" UI field (e.g. `TK_IOS_DEV`) to the iOS
target. It MUST be its own step and run FIRST: changing scripting defines
triggers a recompile, so the code behind `#if TK_IOS_DEV` (which attaches the
CCD auth header) is only active on the NEXT Unity launch — i.e. steps 1+.
Without it, CCD content requests get HTTP 401 and the app can't load content.
Set the "Scripting defines" field in Edit -> Content/CCD to `TK_IOS_DEV` (beta).

### Step 1 — blob + Addressables (type: executeMethod)
- name:  `blob + addressables`
- type:  `executeMethod`
- value: `VoxelMage.EditorTools.CIBuild.BuildBlobAndAddressables`

This builds the blob, then the Addressables bundles, into the profile's
RemoteBuildPath (e.g. `ServerData/iOS` or `AssetBundles/iOS` depending on your
profile). Note that path — the upload step syncs it.

### Step 2 — upload to CCD (type: shell)
- name:  `ccd upload`
- type:  `shell`
- value: paste this as a SINGLE LINE (no backslashes / line continuations — the
  UI stores the step value as one shell string, and mangled continuations inject
  stray empty args that `ugs` rejects with "' ' was not matched"). This version
  first checks the build dir exists and is non-empty, so a blank/unsaved Remote
  build path fails with a clear message instead of ugs's "Directory '' does not
  exist":

      test -d "$UBS_REMOTE_BUILD_PATH" && [ -n "$(ls -A "$UBS_REMOTE_BUILD_PATH" 2>/dev/null)" ] || { echo "Remote build path '$UBS_REMOTE_BUILD_PATH' is empty or unset — did Addressables build there, and is the field set in the UI?"; exit 1; }; ugs ccd entries sync "$UBS_REMOTE_BUILD_PATH" -p "$UBS_CCD_PROJECT_ID" -e "$UBS_CCD_ENVIRONMENT" -b "$UBS_CCD_BUCKET" --create-release

  (If you'd rather keep it minimal, the bare command also works once the field
  is set: `ugs ccd entries sync "$UBS_REMOTE_BUILD_PATH" -p "$UBS_CCD_PROJECT_ID" -e "$UBS_CCD_ENVIRONMENT" -b "$UBS_CCD_BUCKET" --create-release`)

  ONE command does it all: `ugs ccd entries sync ... --create-release` uploads
  the bundles AND cuts a release in a single call (the `sync` subcommand has a
  `-r`/`--create-release` flag — simpler than a separate `releases create`).
  `--create-release` auto-moves the `latest` badge to the new release, so you do
  NOT create a badge yourself (CCD reserves `latest`). To set a DIFFERENT badge,
  add `-u <badge-name>` (only valid with --create-release).

  It reads the per-project values from env vars the server sets from each
  project's UI fields, so you paste the SAME line into all four projects — only
  the UI field values differ.

  IMPORTANT — `-b`/`--bucket-name` wants the bucket's NAME, not its UUID. Run
  `ugs ccd buckets list` to see names; e.g. voxelmage's bucket is named `iOS`.

  Short flags (confirmed from `ugs ccd entries sync -h`):
    -p project-id   -e environment-name   -b bucket-name
    -r/--create-release   -u badge   -x delete-missing   -v verbose

  The shared service-account auth (UGS_CLI_SERVICE_KEY_ID /
  UGS_CLI_SERVICE_SECRET_KEY) stays in the host/launchd env — it's org-level and
  the same for all projects.

Steps run in order and the build ABORTS on the first failure, so a blob,
Addressables, or upload failure stops the build before it ships an app with
missing content.

## Verified `ugs ccd` command reference (from Unity docs)

    ugs login | logout | login status
    ugs env use <environment-name>
    ugs config set project-id <project-id>
    ugs config set bucket-name <bucket-name>
    ugs ccd entries sync <local-dir>        # upload a folder of bundles
    ugs ccd entries list [-b <bucket>]
    ugs ccd releases create                 # release from latest bucket version
    ugs ccd releases list / info <num>
    ugs ccd badges create <release-num> <badge-name>
    ugs ccd badges list / delete <badge-name>

Global flags: -h/--help, -q/--quiet, -j/--json (default output is YAML).
Env vars: UGS_CLI_SERVICE_KEY_ID, UGS_CLI_SERVICE_SECRET_KEY,
UGS_CLI_PROJECT_ID, UGS_CLI_ENVIRONMENT_NAME, UGS_CLI_BUCKET_NAME.

Full walkthrough: https://docs.unity.com/en-us/ccd/cli-walkthrough

## Why not do CCD in C#?

The CCD Management package you have (3.0.3) only works inside the editor GUI —
in batchmode it throws "Cloud Services must be enabled and connected to a Unity
Cloud Project" because the project binding never syncs. Unity staff confirm it's
editor-only. The `ugs` CLI is the supported path for build pipelines.
