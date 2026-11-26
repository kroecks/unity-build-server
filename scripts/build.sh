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
#
# Platform (set by the server from the project's `platform` field):
#   UBS_PLATFORM         "ios" | "android"   (Quest = android)
#   UBS_BUILD_TARGET     Unity -buildTarget: iOS | Android
#   UBS_ANDROID_AAB      "1" = .aab (Play), else .apk (adb / Quest)
#   UBS_KEYSTORE_PATH / UBS_KEYSTORE_ALIAS / UBS_KEYSTORE_PASS / UBS_KEY_ALIAS_PASS
#                        release signing; blank path = Unity debug keystore
#   UBS_ADB              adb binary (server resolves it); UBS_DEVICE_UDID is the
#                        adb serial on Android (host:port = ADB-over-WiFi)

set -euo pipefail

PROJECT_DIR="$UBS_PROJECT_DIR"
BRANCH="${UBS_BRANCH:-main}"
DEPLOY="${UBS_DEPLOY:-0}"
OUTPUT_DIR="${UBS_OUTPUT_DIR:-$HOME/builds/output}"
XCODE_BUILD_DIR="${UBS_XCODE_BUILD_DIR:-$OUTPUT_DIR/xcode}"
PLATFORM="${UBS_PLATFORM:-ios}"
case "$PLATFORM" in
  ios)     BUILD_TARGET="${UBS_BUILD_TARGET:-iOS}" ;;
  android) BUILD_TARGET="${UBS_BUILD_TARGET:-Android}" ;;
  *) printf 'ERROR: unknown UBS_PLATFORM %q\n' "$PLATFORM" >&2; exit 1 ;;
esac
ANDROID_OUT_DIR="$OUTPUT_DIR/android"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# launchd gives a minimal PATH; make sure brew + unity + xcode tools are found.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

log() { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }
mkdir -p "$OUTPUT_DIR"

# --- Android / Quest helpers (adb) ------------------------------------------ #
ADB="${UBS_ADB:-}"
resolve_adb() {
  [[ -n "$ADB" && -x "$ADB" ]] && return 0
  ADB="$(command -v adb 2>/dev/null || true)"
  [[ -n "$ADB" ]] && return 0
  # Fall back to the SDK bundled with a Hub editor's Android module.
  ADB="$(ls -1d "$HOME"/Applications/Unity/Hub/Editor/*/PlaybackEngines/AndroidPlayer/SDK/platform-tools/adb \
                 /Applications/Unity/Hub/Editor/*/PlaybackEngines/AndroidPlayer/SDK/platform-tools/adb \
                 2>/dev/null | sort -r | head -n1 || true)"
  [[ -n "$ADB" ]]
}

# 0 if the adb serial is connected and authorized. Connects first for host:port.
android_device_ready() {
  local serial="$1" state
  resolve_adb || { log "adb not found (brew install android-platform-tools, or set adb_path)."; return 1; }
  if [[ "$serial" == *:* ]]; then
    log "ADB-over-WiFi: adb connect $serial"
    "$ADB" connect "$serial" 2>&1 | sed 's/^/    /' || true
  fi
  "$ADB" devices -l 2>&1 | sed 's/^/    /' || true
  state="$("$ADB" -s "$serial" get-state 2>&1 || true)"
  case "$state" in
    device) return 0 ;;
    *unauthorized*)
      log "Device $serial is UNAUTHORIZED."
      echo "  Put on the headset / unlock the phone and accept 'Allow USB debugging'" >&2
      echo "  (tick 'Always allow from this computer')." >&2 ;;
    *)
      log "Device $serial not reachable (state: ${state:-none})."
      echo "  USB: cable in, Developer Mode on (Quest: via the Meta Horizon app)." >&2
      echo "  WiFi: once over USB run 'adb tcpip 5555', then use <ip>:5555 as the serial." >&2 ;;
  esac
  return 1
}

# Install an APK via adb, classifying failures so only transient ones retry.
install_apk_to_device() {
  local apk="$1" serial="$UBS_DEVICE_UDID" attempt out rc
  if [[ "$apk" == *.aab ]]; then
    log "INSTALL SKIPPED: $apk is an App Bundle (.aab) — adb can't install it."
    echo "  Untick 'Build AAB' to get an installable .apk, or use bundletool." >&2
    return 1
  fi
  android_device_ready "$serial" || return 1
  log "Installing $apk to $serial"
  for attempt in 1 2 3; do
    set +e
    out="$("$ADB" -s "$serial" install -r "$apk" 2>&1)"; rc=$?
    set -e
    printf '%s\n' "$out"
    # adb sometimes exits 0 while printing Failure — check the text too.
    if [[ $rc -eq 0 ]] && ! printf '%s' "$out" | grep -q "Failure"; then
      return 0
    fi
    if printf '%s' "$out" | grep -qE "INSTALL_FAILED_UPDATE_INCOMPATIBLE|signatures do not match"; then
      log "INSTALL FAILED: the installed app is signed with a DIFFERENT key."
      echo "  (e.g. a debug-keystore build over a release-signed one, or vice versa.)" >&2
      echo "  Uninstall it first — this WIPES the app's data on the device:" >&2
      echo "    $ADB -s $serial uninstall ${UBS_BUNDLE_ID:-<package>}" >&2
      echo "  then tap Install on this build. Not done automatically to protect saves." >&2
      return 1
    fi
    if printf '%s' "$out" | grep -qE "INSTALL_FAILED_VERSION_DOWNGRADE"; then
      log "INSTALL FAILED: device has a HIGHER versionCode than this build."
      echo "  Uninstall the app on the device, or bump next_build_number past it." >&2
      return 1
    fi
    if printf '%s' "$out" | grep -qE "INSTALL_FAILED_INSUFFICIENT_STORAGE|not enough space"; then
      log "INSTALL FAILED: the DEVICE is out of storage. Free space, then tap Install."
      return 1
    fi
    if printf '%s' "$out" | grep -qE "INSTALL_FAILED_NO_MATCHING_ABIS|INSTALL_PARSE_FAILED|INSTALL_FAILED_OLDER_SDK"; then
      log "INSTALL FAILED: APK incompatible with this device (ABI / minSdk / bad package)."
      echo "  Quest needs ARM64 + IL2CPP. Fix Player Settings and rebuild." >&2
      return 1
    fi
    log "Install attempt $attempt failed (transient?). Retrying in 5s…"
    sleep 5
    android_device_ready "$serial" >/dev/null 2>&1 || true
  done
  log "INSTALL FAILED after 3 tries."
  return 1
}

launch_android_app() {
  [[ -n "${UBS_BUNDLE_ID:-}" ]] || { log "No package name set — not launching."; return 0; }
  "$ADB" -s "$UBS_DEVICE_UDID" shell monkey -p "$UBS_BUNDLE_ID" \
    -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 \
    || log "  (launch failed — start it from the device; on Quest: Library → Unknown Sources)"
}

# Install a .app to the device, classifying failures so we retry only the
# transient ones. Returns 0 on success, 1 on give-up. Needs UBS_DEVICE_UDID.
install_app_to_device() {
  local app="$1" attempt out
  log "Installing $app"
  for attempt in 1 2 3; do
    # Capture output so we can read the REAL reason (devicectl buries it inside
    # a generic 3002). tee keeps it visible in the live log too.
    out="$(xcrun devicectl device install app --device "$UBS_DEVICE_UDID" "$app" 2>&1)"
    local rc=$?
    printf '%s\n' "$out"
    if [[ $rc -eq 0 ]]; then
      return 0
    fi
    # Disk-full never succeeds on retry — stop immediately with the real numbers.
    if printf '%s' "$out" | grep -qiE "Insufficient storage|not enough space"; then
      log "INSTALL FAILED: the PHONE is out of storage."
      printf '%s\n' "$out" | grep -iE "bytes needed|available|DiskSpaceRequired" | sed 's/^/    /' >&2
      echo "  Free up space on the iPhone (the app needs well over 700 MB to stage)," >&2
      echo "  then tap Install on this build in the UI. No rebuild needed." >&2
      return 1
    fi
    # Packaging errors never succeed on retry either — stop and name the real cause.
    if printf '%s' "$out" | grep -qiE "DuplicateIdentifier|same identifier"; then
      log "INSTALL FAILED: bundle identifier collision (a framework shares the app's id)."
      printf '%s\n' "$out" | grep -iE "same identifier|DuplicateIdentifier" | sed 's/^/    /' >&2
      echo "  This is a build-packaging bug, not a device issue. Rebuild is required." >&2
      return 1
    fi
    if printf '%s' "$out" | grep -qiE "This application's application-identifier entitlement|does not match|provisioning profile"; then
      log "INSTALL FAILED: signing/provisioning mismatch — rebuild required."
      printf '%s\n' "$out" | grep -iE "entitlement|provisioning|identifier" | head -4 | sed 's/^/    /' >&2
      return 1
    fi
    # Otherwise treat as transient (sleep/lock/network) and retry.
    log "Install attempt $attempt failed (transient). Make sure the phone is unlocked & awake. Retrying in 5s…"
    sleep 5
  done
  log "INSTALL FAILED after 3 tries — phone locked, asleep, or off-network during transfer."
  echo "  Unlock the phone (keep it awake) and tap Install on this build in the UI." >&2
  return 1
}

# --- Install-only short-circuit -------------------------------------------- #
# The UI "Install" button re-pushes an already-built artifact to the phone
# without rebuilding. We skip straight to the install, with the same retries.
if [[ "${UBS_INSTALL_ONLY:-0}" == "1" ]]; then
  ART="${UBS_INSTALL_ARTIFACT:-}"
  log "Install-only: pushing existing artifact to device"
  [[ -n "$UBS_DEVICE_UDID" ]] || { log "ERROR: no device UDID / adb serial set."; exit 1; }
  [[ -n "$ART" && -e "$ART" ]] || { log "ERROR: artifact not found: $ART"; exit 1; }
  if [[ "$PLATFORM" == "android" ]]; then
    if install_apk_to_device "$ART"; then
      log "Installed. Launching…"; launch_android_app
      log "DONE"; exit 0
    fi
    exit 1
  fi
  # If the artifact is an .xcarchive or .ipa we need the .app inside; devicectl
  # installs a .app bundle. Find one if we were handed a container.
  if [[ "$ART" == *.app ]]; then
    APP_PATH="$ART"
  else
    APP_PATH="$(/usr/bin/find "$(dirname "$ART")" -name '*.app' -type d -maxdepth 5 2>/dev/null | head -n1)"
  fi
  [[ -n "$APP_PATH" && -d "$APP_PATH" ]] || { log "ERROR: no .app found to install from $ART"; exit 1; }
  if install_app_to_device "$APP_PATH"; then
    log "Installed. Launching…"
    xcrun devicectl device process launch --device "$UBS_DEVICE_UDID" "$UBS_BUNDLE_ID" || true
    log "DONE"; exit 0
  fi
  exit 1
fi

# --- Distribute-only short-circuit ----------------------------------------- #
# The UI "Distribute" button re-exports an existing .xcarchive for App Store
# distribution and uploads it to TestFlight via the ASC API key. No rebuild.
if [[ "${UBS_DISTRIBUTE:-0}" == "1" ]]; then
  if [[ "$PLATFORM" != "ios" ]]; then
    log "ERROR: Distribute is TestFlight-only; not supported for $PLATFORM yet."; exit 1
  fi
  ARCH="${UBS_DISTRIBUTE_ARCHIVE:-}"
  log "Distribute: exporting archive for App Store and uploading to TestFlight"
  [[ -n "$ARCH" && -d "$ARCH" ]] || { log "ERROR: no .xcarchive at: $ARCH"; exit 1; }
  for v in UBS_ASC_KEY_ID UBS_ASC_ISSUER_ID UBS_ASC_KEY_PATH UBS_DEVELOPMENT_TEAM; do
    [[ -n "${!v:-}" ]] || { log "ERROR: $v is not set (App Store Connect key / team)."; exit 1; }
  done
  [[ -f "$UBS_ASC_KEY_PATH" ]] || { log "ERROR: ASC key file not found: $UBS_ASC_KEY_PATH"; exit 1; }

  EXPORT_DIR="${UBS_OUTPUT_DIR:-$(dirname "$ARCH")}/export"
  rm -rf "$EXPORT_DIR"; mkdir -p "$EXPORT_DIR"
  PLIST="$EXPORT_DIR/ExportOptions.plist"
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>app-store-connect</string>
  <key>teamID</key><string>$UBS_DEVELOPMENT_TEAM</string>
  <key>signingStyle</key><string>automatic</string>
  <key>destination</key><string>export</string>
  <key>uploadSymbols</key><true/>
</dict></plist>
PL

  log "xcodebuild -exportArchive (App Store distribution signing)"
  if ! xcodebuild -exportArchive \
      -archivePath "$ARCH" \
      -exportOptionsPlist "$PLIST" \
      -exportPath "$EXPORT_DIR" \
      -allowProvisioningUpdates \
      -authenticationKeyID "$UBS_ASC_KEY_ID" \
      -authenticationKeyIssuerID "$UBS_ASC_ISSUER_ID" \
      -authenticationKeyPath "$UBS_ASC_KEY_PATH"; then
    log "ERROR: exportArchive failed. If it says 'No Accounts with App Store Connect"
    echo "  Access' or auth -26000, your API key must be a TEAM key, not Individual." >&2
    exit 1
  fi

  IPA="$(/usr/bin/find "$EXPORT_DIR" -name '*.ipa' -maxdepth 2 | head -n1)"
  [[ -n "$IPA" && -f "$IPA" ]] || { log "ERROR: no .ipa produced by exportArchive."; exit 1; }
  # altool looks for AuthKey_<id>.p8 in a private_keys dir; point it at ours.
  export API_PRIVATE_KEYS_DIR="$(dirname "$UBS_ASC_KEY_PATH")"
  log "Uploading $IPA to App Store Connect / TestFlight"
  if ! xcrun altool --upload-app -t ios -f "$IPA" \
      --apiKey "$UBS_ASC_KEY_ID" --apiIssuer "$UBS_ASC_ISSUER_ID"; then
    log "ERROR: upload failed. Check the API key is a TEAM key with App Manager role,"
    echo "  and that the build version/bundle id are valid for this app in ASC." >&2
    exit 1
  fi
  log "DONE — uploaded to TestFlight. Processing on Apple's side takes a few minutes."
  exit 0
fi

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
# -f: this dir is a disposable mirror, so discard local edits / untracked files
# that are "in the way" instead of refusing. Without it, a file Unity modified
# that upstream later deleted (or untracked + gitignored) blocks the checkout
# with "would be overwritten", and the reset below never gets to run.
git merge --abort 2>/dev/null || true
git checkout -f -B "$BRANCH" "origin/$BRANCH"
git reset --hard "origin/$BRANCH"
if [[ "${UBS_CLEAN_IGNORED:-0}" == "1" ]]; then
  git clean -fdx   # removes Library/ too — slow next build
else
  git clean -fd    # keep ignored files (Library/ survives) — fast
fi
git log -1 --oneline

# --- 1a. Optional merge of another branch into this one -------------------- #
# e.g. the 'ios' branch merges in 'main' so a push to main gets built on ios.
# CLEAN/fast-forward ONLY: a conflict aborts the merge AND the build, leaving
# origin untouched, so you resolve it by hand. A successful merge is pushed back
# to origin/<branch> so it persists (the build dir is reset each run).
if [[ -n "${UBS_MERGE_FROM:-}" && "$UBS_MERGE_FROM" != "$BRANCH" ]]; then
  MF="$UBS_MERGE_FROM"
  log "Merging origin/$MF into $BRANCH (clean-only; conflict will abort)"
  if ! git rev-parse --verify --quiet "origin/$MF" >/dev/null; then
    log "ERROR: merge source 'origin/$MF' does not exist."; exit 1
  fi
  # If already up to date (nothing to merge), this is a no-op and succeeds.
  if git merge --no-edit --no-ff "origin/$MF"; then
    # Did the merge actually add commits? If so, push it back so it persists.
    if [[ -n "$(git rev-list "origin/$BRANCH..HEAD" 2>/dev/null)" ]]; then
      log "Merge clean — pushing updated $BRANCH back to origin"
      if ! git push origin "$BRANCH"; then
        log "WARNING: merge succeeded locally but push to origin/$BRANCH failed."
        echo "  Building the merged tree anyway, but origin was NOT updated." >&2
      fi
    else
      log "$BRANCH already contains origin/$MF — nothing to merge."
    fi
  else
    # Conflict (or other merge failure): abort cleanly and stop. Do NOT build a
    # half-merged tree, and do NOT touch origin.
    git merge --abort 2>/dev/null || true
    log "MERGE CONFLICT: origin/$MF does not merge cleanly into $BRANCH."
    echo "  The build is aborted and origin/$BRANCH is untouched." >&2
    echo "  Resolve by hand:  git checkout $BRANCH && git merge origin/$MF" >&2
    echo "  …fix conflicts, commit, push, then rebuild." >&2
    git status --short | sed 's/^/    /' >&2 || true
    exit 2
  fi
  git log -1 --oneline
fi

# Capture the commit actually being built (AFTER any merge), for build-info.json.
BUILD_COMMIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo "")"

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

# Platform build-support modules. Android needs the SDK/NDK and OpenJDK child
# modules too, or the Gradle step fails. Override with UBS_UNITY_MODULES
# (space-separated) if your CLI uses different module ids.
if [[ -n "${UBS_UNITY_MODULES:-}" ]]; then
  read -r -a UNITY_MODULES <<< "$UBS_UNITY_MODULES"
elif [[ "$PLATFORM" == "android" ]]; then
  UNITY_MODULES=(android android-sdk-ndk-tools android-open-jdk)
else
  UNITY_MODULES=(ios)
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
  log "Installing Unity $TARGET_VERSION with modules: ${UNITY_MODULES[*]}"
  unity install "$TARGET_VERSION" -m "${UNITY_MODULES[@]}"
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

# --- 2a. Verify the platform build-support module, not just the version ---- #
# An editor can be installed (fast path above) yet lack this platform's module,
# e.g. installed earlier for iOS only. Module presence = its PlaybackEngines dir,
# which sits next to Unity.app (Hub layout) or inside it (older layouts).
EDITOR_ROOT="$(cd "$(dirname "$UNITY_EXEC")/../../.." && pwd)"   # dir containing Unity.app
module_missing() {
  local pe
  for pe in "$EDITOR_ROOT/PlaybackEngines" "$EDITOR_ROOT/Unity.app/Contents/PlaybackEngines"; do
    if [[ "$PLATFORM" == "android" ]]; then
      [[ -d "$pe/AndroidPlayer" && -d "$pe/AndroidPlayer/SDK" && -d "$pe/AndroidPlayer/NDK" \
         && -d "$pe/AndroidPlayer/OpenJDK" ]] && { echo ""; return 1; }
    else
      [[ -d "$pe/iOSSupport" ]] && { echo ""; return 1; }
    fi
  done
  if [[ "$PLATFORM" == "android" ]]; then
    echo "Android Build Support (+ Android SDK & NDK Tools + OpenJDK)"
  else
    echo "iOS Build Support"
  fi
  return 0
}
if MISSING="$(module_missing)"; then
  log "Unity $TARGET_VERSION is missing: $MISSING — installing module(s) ${UNITY_MODULES[*]}"
  unity install "$TARGET_VERSION" -m "${UNITY_MODULES[@]}" || true
  if MISSING="$(module_missing)"; then
    log "ERROR: $MISSING is still not installed for Unity $TARGET_VERSION."
    echo "  Looked under: $EDITOR_ROOT/PlaybackEngines" >&2
    echo "  Add it in Unity Hub → Installs → $TARGET_VERSION → Add modules, then rebuild." >&2
    exit 1
  fi
fi
log "$BUILD_TARGET build support present"

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
    -buildTarget "$BUILD_TARGET" -logFile "$ulog" "$@"
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

# --- 3. Unity batchmode build -> Xcode project (iOS) / APK|AAB (Android) --- #
log "Running Unity batchmode build ($BUILD_TARGET)"
if [[ "$PLATFORM" == "android" ]]; then
  # Release signing: if a keystore is configured it MUST be complete — never
  # silently fall back to the debug key for a build you meant to sign.
  if [[ -n "${UBS_KEYSTORE_PATH:-}" ]]; then
    [[ -f "$UBS_KEYSTORE_PATH" ]] || { log "ERROR: keystore not found: $UBS_KEYSTORE_PATH"; exit 1; }
    [[ -n "${UBS_KEYSTORE_ALIAS:-}" ]] || { log "ERROR: keystore set but no key alias."; exit 1; }
    if [[ -z "${UBS_KEYSTORE_PASS:-}" ]]; then
      log "ERROR: no password for keystore $UBS_KEYSTORE_PATH."
      echo "  Add it to config.json under \"android_keystores\" (keyed by that path, or" >&2
      echo "  \"default\"), then restart the server." >&2
      exit 1
    fi
    log "Signing with release keystore $(basename "$UBS_KEYSTORE_PATH") (alias $UBS_KEYSTORE_ALIAS)"
  else
    log "No keystore configured — Unity debug keystore (fine for sideloading / Quest dev)"
  fi
  EXT="apk"; [[ "${UBS_ANDROID_AAB:-0}" == "1" ]] && EXT="aab"
  rm -rf "$ANDROID_OUT_DIR"; mkdir -p "$ANDROID_OUT_DIR"
  # Name the file after package + build number so downloads are self-describing.
  ART_NAME="$(printf '%s' "${UBS_BUNDLE_ID:-app}" | tr -c 'A-Za-z0-9._-' '_')-${UBS_BUILD_NUMBER:-local}.$EXT"
  PLAYER_OUT="$ANDROID_OUT_DIR/$ART_NAME"
  UNITY_LOG="$OUTPUT_DIR/android.unity-build.log"
else
  rm -rf "$XCODE_BUILD_DIR"
  PLAYER_OUT="$XCODE_BUILD_DIR"
  UNITY_LOG="${XCODE_BUILD_DIR%/}.unity-build.log"
fi

# Unity in batchmode wants to open and own its log file; handing it /dev/stdout
# fails on macOS ("Unable to open log file, exiting"). So we log to a real file
# and tail it into our output, which gives the live stream without the handle
# problem. We background a tailer, run Unity, then stop the tailer.
mkdir -p "$(dirname "$UNITY_LOG")"
: > "$UNITY_LOG"
tail -n +1 -f "$UNITY_LOG" & TAIL_PID=$!
# Make sure the tailer dies even if Unity crashes the script.
trap '[[ -n "${TAIL_PID:-}" ]] && kill "$TAIL_PID" 2>/dev/null || true' EXIT

set +e
"$UNITY_EXEC" -batchmode -nographics -quit \
  -projectPath "$PROJECT_DIR" \
  -executeMethod "${UBS_BUILD_METHOD:-BuildScript.PerformiOSBuild}" \
  -buildTarget "$BUILD_TARGET" -logFile "$UNITY_LOG" \
  -customBuildPath "$PLAYER_OUT"
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

# Record version/build-number/commit for the server to read back and display.
# bundleVersion lives in ProjectSettings.asset (YAML: "  bundleVersion: 1.4.2").
BUILD_VERSION="$(grep -m1 '  bundleVersion:' "$PROJECT_DIR/ProjectSettings/ProjectSettings.asset" 2>/dev/null \
  | sed 's/.*bundleVersion:[[:space:]]*//' | tr -d '\r' || echo "")"
python3 -I - "$OUTPUT_DIR/build-info.json" <<PYEOF || true
import json, sys
json.dump({
    "version": "${BUILD_VERSION}".strip(),
    "build_number": "${UBS_BUILD_NUMBER:-}".strip(),
    "commit_sha": "${BUILD_COMMIT_SHA:-}".strip(),
}, open(sys.argv[1], "w"), indent=2)
PYEOF
log "Recorded build-info.json: version=${BUILD_VERSION} build=${UBS_BUILD_NUMBER:-} commit=${BUILD_COMMIT_SHA:-}"

# --- 4a. Android: Unity already produced the APK/AAB; optionally adb install - #
if [[ "$PLATFORM" == "android" ]]; then
  if [[ ! -f "$PLAYER_OUT" ]]; then
    # Unity occasionally adjusts the name; take whatever it wrote.
    PLAYER_OUT="$(ls -1t "$ANDROID_OUT_DIR"/*.apk "$ANDROID_OUT_DIR"/*.aab 2>/dev/null | head -n1 || true)"
  fi
  [[ -n "$PLAYER_OUT" && -f "$PLAYER_OUT" ]] || {
    log "ERROR: Unity reported success but no .apk/.aab is in $ANDROID_OUT_DIR"; exit 1; }
  log "Artifact: $PLAYER_OUT ($(du -h "$PLAYER_OUT" | cut -f1))"
  if [[ "$DEPLOY" == "1" ]]; then
    if [[ -z "${UBS_DEVICE_UDID:-}" ]]; then
      log "Deploy requested but NO adb serial is set for this project."
      resolve_adb && { echo "  Connected devices right now:" >&2; "$ADB" devices -l 2>&1 | sed 's/^/    /' >&2; } || true
    elif install_apk_to_device "$PLAYER_OUT"; then
      log "Installed to device. Launching…"; launch_android_app
    else
      # Same as iOS: the build is good; Install can push it later.
      log "DONE (build succeeded, install pending — see reason above)"; exit 0
    fi
  fi
  log "DONE"; exit 0
fi

# --- 4. Xcode compile/sign + optional device install ----------------------- #
log "Building Xcode project"
cd "$XCODE_BUILD_DIR"
if [[ -d "Unity-iPhone.xcworkspace" ]]; then
  SCHEME_FLAG=(-workspace "Unity-iPhone.xcworkspace")
else
  SCHEME_FLAG=(-project "Unity-iPhone.xcodeproj")
fi

# Device detection — made explicit and loud so a missed device is visible in the
# log instead of silently falling through to "archive only".
DEVICE_AVAILABLE=0
if [[ "$DEPLOY" == "1" ]]; then
  if [[ -z "${UBS_DEVICE_UDID:-}" ]]; then
    log "Deploy requested but NO device UDID is set for this project."
    echo "  Set the Device UDID in the project's settings to install to your phone." >&2
    echo "  Connected devices right now:" >&2
    xcrun devicectl list devices 2>&1 | sed 's/^/    /' >&2 || true
  else
    log "Deploy requested — looking for device $UBS_DEVICE_UDID"
    # Use devicectl's JSON output and match the UDID exactly against a CONNECTED
    # physical device. The pretty-printed table appends " (UDID)" and lists
    # shutdown simulators, so JSON is far more reliable to parse.
    DEV_JSON="$(xcrun devicectl list devices --json-output - 2>/dev/null || true)"
    # Show the human-readable list in the log for visibility.
    xcrun devicectl list devices 2>/dev/null | sed 's/^/    /' || true
    MATCH="$(printf '%s' "$DEV_JSON" | python3 -I -c '
import sys, json
want = sys.argv[1]
try: data = json.load(sys.stdin)
except Exception: sys.exit(1)
devs = (data.get("result") or {}).get("devices") or []
for d in devs:
    udid = (d.get("hardwareProperties") or {}).get("udid") or d.get("identifier") or ""
    state = (d.get("connectionProperties") or {}).get("tunnelState") \
            or (d.get("deviceProperties") or {}).get("connectionState") or ""
    if udid == want:
        print("found"); sys.exit(0)
sys.exit(1)
' "$UBS_DEVICE_UDID" 2>/dev/null || true)"
    if [[ "$MATCH" == "found" ]]; then
      DEVICE_AVAILABLE=1
      log "Device $UBS_DEVICE_UDID found — will install after build."
    else
      # Fall back to a plain substring match in case the JSON shape differs on
      # this Xcode version — better to try the install than wrongly archive.
      if xcrun devicectl list devices 2>/dev/null | grep -q "$UBS_DEVICE_UDID"; then
        DEVICE_AVAILABLE=1
        log "Device $UBS_DEVICE_UDID found (table match) — will install after build."
      else
        log "Device $UBS_DEVICE_UDID NOT found above."
        echo "  Check: phone plugged in, unlocked, trusted, Developer Mode on." >&2
        echo "  The UDID must match exactly (incl. the dash)." >&2
      fi
    fi
  fi
fi

# Automatic signing — this is the command-line equivalent of opening Xcode,
# picking your team, and letting it manage the provisioning profile for you.
# The Unity-generated project defaults to MANUAL signing (which demands a named
# profile and fails with "requires a provisioning profile"); CODE_SIGN_STYLE=
# Automatic + DEVELOPMENT_TEAM + -allowProvisioningUpdates makes xcodebuild
# create/download the managed profile and sign with it, no GUI step.
SIGN_SETTINGS=(
  -allowProvisioningUpdates
  CODE_SIGN_STYLE=Automatic
  DEVELOPMENT_TEAM="$UBS_DEVELOPMENT_TEAM"
)
# NOTE: we deliberately do NOT pass PRODUCT_BUNDLE_IDENTIFIER here. A build
# setting on the xcodebuild command line applies to EVERY target, which stamps
# the app's bundle id onto UnityFramework.framework too — iOS then rejects the
# install with "DuplicateIdentifier: parent bundle has the same identifier as
# sub-bundle". Unity already sets the correct per-target bundle ids in the
# project, so we leave them alone and only drive signing.

if [[ -z "${UBS_DEVELOPMENT_TEAM:-}" ]]; then
  log "ERROR: no Apple Team ID set for this project — signing will fail."
  echo "  Set the Team ID in the project's settings in the UI." >&2
  exit 1
fi

# Keep ALL Xcode intermediates on the (roomy) output volume, not the mini's
# small internal drive. For an IL2CPP Unity build, DerivedData is several GB per
# build; without this it defaults to ~/Library/Developer/Xcode/DerivedData on
# the internal disk and fills it. Both build and archive use this path, and it
# lives under the per-build output dir so retention cleans it up too.
DERIVED_DATA="$XCODE_BUILD_DIR/DerivedData"
mkdir -p "$DERIVED_DATA"

if [[ "$DEPLOY" == "1" && "$DEVICE_AVAILABLE" == "1" ]]; then
  log "Device available — building for device and installing (automatic signing)"
  xcodebuild "${SCHEME_FLAG[@]}" -scheme Unity-iPhone -configuration Release \
    -destination "id=$UBS_DEVICE_UDID" "${SIGN_SETTINGS[@]}" \
    -derivedDataPath "$DERIVED_DATA" build
  APP_PATH="$(/usr/bin/find "$DERIVED_DATA" -name '*.app' -maxdepth 5 -type d | head -n1)"
  # Also archive, so a successful device build is still promotable to TestFlight
  # and downloadable even if the phone install step can't land right now.
  log "Archiving the signed build for distribution/download"
  xcodebuild "${SCHEME_FLAG[@]}" -scheme Unity-iPhone -configuration Release -sdk iphoneos \
    "${SIGN_SETTINGS[@]}" -derivedDataPath "$DERIVED_DATA" \
    -archivePath "$XCODE_BUILD_DIR/build.xcarchive" archive \
    >/dev/null 2>&1 || log "  (archive step skipped/failed — device build still OK)"

  if install_app_to_device "$APP_PATH"; then
    log "Installed to device. Launching…"
    xcrun devicectl device process launch --device "$UBS_DEVICE_UDID" "$UBS_BUNDLE_ID" || true
    log "DONE"
  else
    # Build succeeded; only the install didn't land. Don't fail the whole build —
    # the artifact is good, and the UI's Install button can push it later.
    log "DONE (build succeeded, install pending — see reason above)"
  fi
  exit 0
else
  [[ "$DEPLOY" == "1" ]] && log "Deploy requested but device not reachable — archiving" || log "Archiving only"
  xcodebuild "${SCHEME_FLAG[@]}" -scheme Unity-iPhone -configuration Release -sdk iphoneos \
    "${SIGN_SETTINGS[@]}" -derivedDataPath "$DERIVED_DATA" \
    -archivePath "$XCODE_BUILD_DIR/build.xcarchive" archive
  log "Archive at $XCODE_BUILD_DIR/build.xcarchive"
fi
log "DONE"