#!/bin/bash
# Unity build + iOS deploy, invoked by the server once per build (one at a time).
# ALL inputs come from the environment, set per-project by the server from the
# registry. There is no per-project env.sh to hand-edit anymore.
#
#   UBS_PROJECT_DIR      the cloned project on disk
#   UBS_BRANCH           branch to build
#   UBS_DEPLOY           "1" to install to the device, "0" to just archive
#   UBS_BUILD_METHOD     C# static method (BuildScript.PerformiOSBuild)
#   UBS_DEVELOPMENT_TEAM Apple Team ID
#   UBS_BUNDLE_ID        bundle identifier
#   UBS_DEVICE_UDID      target device (may be empty -> archive only)
#   UBS_RECLAIM_DISK     "1" to uninstall other editor versions when installing
#   UBS_CLEAN_IGNORED    "1" for git clean -fdx (nukes Library/)
#   UBS_XCODE_BUILD_DIR  where Unity writes the Xcode project

set -euo pipefail

PROJECT_DIR="$UBS_PROJECT_DIR"
BRANCH="${UBS_BRANCH:-main}"
DEPLOY="${UBS_DEPLOY:-0}"
OUTPUT_DIR="${UBS_OUTPUT_DIR:-$HOME/builds/output}"
XCODE_BUILD_DIR="${UBS_XCODE_BUILD_DIR:-$OUTPUT_DIR/xcode}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# launchd gives a minimal PATH; make sure brew + unity + xcode tools are found.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

log() { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }
mkdir -p "$OUTPUT_DIR"

# --- 1. Sync repo to exactly origin/<branch> ------------------------------- #
# The branch is chosen per project in the UI, so a hard reset to origin/<branch>
# is the intended "match the remote exactly" — it will NOT silently clobber your
# iOS delta as long as that delta lives on the branch you selected. If you build
# a branch whose iOS commits are only local and unpushed, reset discards them;
# push them to the branch first. We guard against building a branch that does
# not exist on the remote at all.
log "Syncing $PROJECT_DIR to origin/$BRANCH"
cd "$PROJECT_DIR"
git fetch --prune origin
if ! git rev-parse --verify --quiet "origin/$BRANCH" >/dev/null; then
  log "ERROR: branch '$BRANCH' does not exist on origin."
  echo "  Push it first, or pick an existing branch in the project's settings." >&2
  echo "  Remote branches:" >&2
  git branch -r --format='    %(refname:short)' >&2 || true
  exit 1
fi
git checkout -B "$BRANCH" "origin/$BRANCH"
git reset --hard "origin/$BRANCH"
if [[ "${UBS_CLEAN_IGNORED:-0}" == "1" ]]; then
  git clean -fdx   # removes Library/ too — slow next build
else
  git clean -fd    # keep ignored files (Library/ survives) — fast
fi
git log -1 --oneline

# --- 1b. Install the build entry point AFTER the clean --------------------- #
# The clean above wipes untracked files, so BuildScript.cs must be (re)written
# here on every build rather than relying on it being committed. The service
# owns this file; the repo need not contain it.
EDITOR_DIR="$PROJECT_DIR/Assets/Editor"
mkdir -p "$EDITOR_DIR"
if [[ -f "$HERE/BuildScript.cs" ]]; then
  cp "$HERE/BuildScript.cs" "$EDITOR_DIR/BuildScript.cs"
  log "Installed BuildScript.cs into Assets/Editor/"
else
  log "ERROR: $HERE/BuildScript.cs not found — cannot install build entry point"
  exit 1
fi

# --- 2. Ensure correct editor (fast path: already installed = do nothing) -- #
TARGET_VERSION="$(awk -F': ' '/m_EditorVersion:/ {print $2; exit}' \
  "$PROJECT_DIR/ProjectSettings/ProjectVersion.txt" | tr -d '[:space:]')"
log "Project targets Unity $TARGET_VERSION"

# Confirm the CLI is authenticated before we rely on it; an unauthenticated
# CLI returns an error string, not a JSON list, which used to crash the parser.
if ! unity auth status >/dev/null 2>&1; then
  log "ERROR: Unity CLI is not signed in."
  echo "  'unity auth status' failed. For unattended builds set a service account:" >&2
  echo "    export UNITY_SERVICE_ACCOUNT_ID=...    export UNITY_SERVICE_ACCOUNT_SECRET=..." >&2
  echo "  (these live in the launchd plist; a hand-run shell won't have them unless you export them)" >&2
  echo "  Or sign in interactively once with: unity auth login" >&2
  exit 1
fi

# Parse the editor list via the shared helper, which normalizes the CLI's
# envelope (data.items, "location" path key) into [{version,path}]. A non-zero
# exit means the output wasn't the expected shape, so we never run uninstalls
# against something we couldn't read. ($HERE set at the top of the script.)
_editors_raw="$(unity editors -i --json 2>/dev/null || true)"
editors_parsed="$(printf '%s' "$_editors_raw" | python3 -I "$HERE/parse_editors.py" 2>/dev/null)" && list_ok=1 || list_ok=0

if [[ "$list_ok" != "1" ]]; then
  log "WARNING: could not parse 'unity editors -i --json' output."
  echo "  Raw output was:" >&2
  printf '  %s\n' "$_editors_raw" >&2
  echo "  Skipping version management and attempting install of $TARGET_VERSION directly." >&2
  editors_parsed="[]"
fi

has_version() { printf '%s' "$editors_parsed" | python3 -I -c '
import sys, json
want = sys.argv[1]
sys.exit(0 if any(e["version"]==want for e in json.load(sys.stdin)) else 1)
' "$1"; }

if has_version "$TARGET_VERSION"; then
  log "Unity $TARGET_VERSION already installed — fast path"
else
  log "Unity $TARGET_VERSION not installed"
  # Only reclaim disk when we could actually READ the list — never guess.
  if [[ "${UBS_RECLAIM_DISK:-1}" == "1" && "$list_ok" == "1" ]]; then
    log "Reclaiming disk: removing other editor versions"
    printf '%s' "$editors_parsed" | python3 -I -c '
import sys, json, subprocess
target = sys.argv[1]
for e in json.load(sys.stdin):
    v = e["version"]
    if v != target:
        print("uninstalling", v); subprocess.run(["unity","uninstall","-e",v], check=False)
' "$TARGET_VERSION"
  fi
  log "Installing Unity $TARGET_VERSION with iOS module"
  unity install "$TARGET_VERSION" -m ios
  # Re-read so we can resolve the path below (same normalizing helper).
  editors_parsed="$(unity editors -i --json 2>/dev/null | python3 -I "$HERE/parse_editors.py" 2>/dev/null || echo '[]')"
fi

UNITY_BIN="$(printf '%s' "$editors_parsed" | python3 -I -c '
import sys, json
want = sys.argv[1]
for e in json.load(sys.stdin):
    if e["version"]==want: print(e.get("path","")); break
' "$TARGET_VERSION" 2>/dev/null)"
UNITY_EXEC="$UNITY_BIN/Contents/MacOS/Unity"
[[ -x "$UNITY_EXEC" ]] || UNITY_EXEC="$HOME/Applications/Unity/Hub/Editor/$TARGET_VERSION/Unity.app/Contents/MacOS/Unity"
if [[ ! -x "$UNITY_EXEC" ]]; then
  log "ERROR: could not locate the Unity executable for $TARGET_VERSION"
  echo "  Looked at: $UNITY_EXEC" >&2
  exit 1
fi
log "Unity executable: $UNITY_EXEC"

# Run a Unity batchmode -executeMethod call, streaming its log live. Unity on
# macOS can't log to /dev/stdout, so we log to a file and tail it. Returns
# Unity's exit code. Args: <method> <logfile>
run_unity_method() {
  local method="$1" ulog="$2"; shift 2
  mkdir -p "$(dirname "$ulog")"; : > "$ulog"
  tail -n +1 -f "$ulog" & local tpid=$!
  local prev_trap; prev_trap="$(trap -p EXIT || true)"
  trap "kill $tpid 2>/dev/null || true" EXIT
  set +e
  "$UNITY_EXEC" -batchmode -nographics -quit \
    -projectPath "$PROJECT_DIR" \
    -executeMethod "$method" \
    -buildTarget iOS -logFile "$ulog" "$@"
  local rc=$?
  set -e
  sleep 1; kill "$tpid" 2>/dev/null || true
  eval "${prev_trap:-trap - EXIT}"
  return $rc
}

# --- 2b. Optional, project-defined pre-build steps ------------------------- #
# These come from the project's config as a JSON array of {name,type,value}.
# type "executeMethod" runs a Unity batchmode method; type "shell" runs a
# command in the project dir. They run IN ORDER and the FIRST failure aborts
# the whole build (e.g. blob build -> CCD upload must both succeed before the
# player build, or the client would ship wrong content). The service knows
# nothing about what these steps do; they're entirely yours.
PRE_STEPS_JSON="${UBS_PRE_BUILD_STEPS:-[]}"
step_count="$(printf '%s' "$PRE_STEPS_JSON" | python3 -I -c 'import sys,json; print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)"
if [[ "$step_count" != "0" ]]; then
  log "Running $step_count pre-build step(s)"
  for i in $(seq 0 $((step_count-1))); do
    s_name="$(printf '%s' "$PRE_STEPS_JSON" | python3 -I -c "import sys,json;print(json.load(sys.stdin)[$i].get('name',''))")"
    s_type="$(printf '%s' "$PRE_STEPS_JSON" | python3 -I -c "import sys,json;print(json.load(sys.stdin)[$i].get('type',''))")"
    s_value="$(printf '%s' "$PRE_STEPS_JSON" | python3 -I -c "import sys,json;print(json.load(sys.stdin)[$i].get('value',''))")"
    log "Pre-build step $((i+1))/$step_count: ${s_name:-$s_value} [$s_type]"
    if [[ "$s_type" == "executeMethod" ]]; then
      if ! run_unity_method "$s_value" "$OUTPUT_DIR/prestep-$((i+1)).unity.log"; then
        log "ERROR: pre-build step '$s_name' (executeMethod $s_value) failed — aborting build."
        tail -n 40 "$OUTPUT_DIR/prestep-$((i+1)).unity.log" 2>/dev/null || true
        exit 1
      fi
    elif [[ "$s_type" == "shell" ]]; then
      # Run in the project dir with the same UBS_* env already exported.
      ( cd "$PROJECT_DIR" && bash -lc "$s_value" ) || {
        log "ERROR: pre-build step '$s_name' (shell) failed — aborting build."; exit 1; }
    else
      log "ERROR: unknown pre-build step type '$s_type' — aborting build."; exit 1
    fi
    log "Pre-build step $((i+1)) OK"
  done
  log "All pre-build steps succeeded"
fi

# --- 3. Unity batchmode build -> Xcode project ----------------------------- #
log "Running Unity batchmode build"
rm -rf "$XCODE_BUILD_DIR"

# Unity in batchmode wants to open and own its log file; handing it /dev/stdout
# fails on macOS ("Unable to open log file, exiting"). So we log to a real file
# and tail it into our output, which gives the live stream without the handle
# problem. We background a tailer, run Unity, then stop the tailer.
UNITY_LOG="${XCODE_BUILD_DIR%/}.unity-build.log"
mkdir -p "$(dirname "$UNITY_LOG")"
: > "$UNITY_LOG"
tail -n +1 -f "$UNITY_LOG" & TAIL_PID=$!
# Make sure the tailer dies even if Unity crashes the script.
trap '[[ -n "${TAIL_PID:-}" ]] && kill "$TAIL_PID" 2>/dev/null || true' EXIT

set +e
"$UNITY_EXEC" -batchmode -nographics -quit \
  -projectPath "$PROJECT_DIR" \
  -executeMethod "${UBS_BUILD_METHOD:-BuildScript.PerformiOSBuild}" \
  -buildTarget iOS -logFile "$UNITY_LOG" \
  -customBuildPath "$XCODE_BUILD_DIR"
UNITY_RC=$?
set -e

# Let the tailer flush the last lines, then stop it.
sleep 1
kill "$TAIL_PID" 2>/dev/null || true
trap - EXIT

if [[ $UNITY_RC -ne 0 ]]; then
  log "Unity build failed (exit $UNITY_RC). Last lines of the Unity log:"
  tail -n 40 "$UNITY_LOG" || true
  exit $UNITY_RC
fi

# --- 4. Xcode compile/sign + optional device install ----------------------- #
log "Building Xcode project"
cd "$XCODE_BUILD_DIR"
if [[ -d "Unity-iPhone.xcworkspace" ]]; then
  SCHEME_FLAG=(-workspace "Unity-iPhone.xcworkspace")
else
  SCHEME_FLAG=(-project "Unity-iPhone.xcodeproj")
fi

DEVICE_AVAILABLE=0
if [[ -n "${UBS_DEVICE_UDID:-}" ]] && xcrun devicectl list devices 2>/dev/null | grep -q "$UBS_DEVICE_UDID"; then
  DEVICE_AVAILABLE=1
fi

if [[ "$DEPLOY" == "1" && "$DEVICE_AVAILABLE" == "1" ]]; then
  log "Device available — building for device and installing"
  xcodebuild "${SCHEME_FLAG[@]}" -scheme Unity-iPhone -configuration Release \
    -destination "id=$UBS_DEVICE_UDID" -allowProvisioningUpdates \
    DEVELOPMENT_TEAM="$UBS_DEVELOPMENT_TEAM" -derivedDataPath ./DerivedData build
  APP_PATH="$(/usr/bin/find ./DerivedData -name '*.app' -maxdepth 4 -type d | head -n1)"
  log "Installing $APP_PATH"
  xcrun devicectl device install app --device "$UBS_DEVICE_UDID" "$APP_PATH"
  xcrun devicectl device process launch --device "$UBS_DEVICE_UDID" "$UBS_BUNDLE_ID" || true
else
  [[ "$DEPLOY" == "1" ]] && log "Deploy requested but device not reachable — archiving" || log "Archiving only"
  xcodebuild "${SCHEME_FLAG[@]}" -scheme Unity-iPhone -configuration Release -sdk iphoneos \
    -allowProvisioningUpdates DEVELOPMENT_TEAM="$UBS_DEVELOPMENT_TEAM" \
    -archivePath "$XCODE_BUILD_DIR/build.xcarchive" archive
  log "Archive at $XCODE_BUILD_DIR/build.xcarchive"
fi
log "DONE"
