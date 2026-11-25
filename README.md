# Unity build machine (Mac mini) — multi-project

A FastAPI service that turns a Mac mini into a Unity iOS build box for **several
projects**, triggered from your phone on the LAN or from Gitea push webhooks.
The service manages projects itself — you onboard through the web UI, and it
clones the repo, detects the Unity version, and installs the build entry point.
No hand-edited scripts per project.

## How onboarding works

Open the UI, tap **+ Onboard a project**, and pick one of two ways:

1. **Clone from Gitea** (recommended): paste a name and the repo URL
   (`http://terronas:3000/etc/game.git`). The service clones it under
   `builds_root`, reads `ProjectSettings/ProjectVersion.txt` to detect the Unity
   version, and writes `Assets/Editor/BuildScript.cs` into the project.
2. **Browse the Mac**: a folder picker that walks the *mini's* filesystem
   (under `browse_root`) for a project you've already cloned. Folders that are
   Unity projects are marked ●. Select one, give it a name, onboard.

Then tap **Edit** on the project card and fill the three things the service
can't infer: **Apple Team ID**, **Bundle ID**, and optionally a **Device UDID**
(blank = build/archive only, no phone install). These are saved by the service
in `config/projects.json` — still no script editing.

> Why no native folder dialog? The UI is a web page shown on your phone; a
> browser can't open a macOS file dialog on the mini. The "Browse the Mac" tab
> is the equivalent — the server lists its own real directories, so you still
> get a working picker, just rendered in the page.

## Triggers

- **Phone button**: tap Build on a project card. Deploys to the phone if a
  device UDID is set and the phone is reachable; otherwise archives.
- **Gitea webhook**: on push, the service matches the payload's repo URL to a
  project and builds it. Webhook builds deploy to the phone only if you tick
  "Deploy to phone on git push" for that project (off by default).

One global queue — only one Unity build runs at a time across all projects
(two builds sharing a Library/ would corrupt it).

## One-time setup on the Mac mini

```bash
xcode-select --install
# install full Xcode from the App Store, then: sudo xcodebuild -license accept
brew install --cask unity-cli
brew install python
pip3 install -r requirements.txt

cp config/config.example.json config/config.json
#   set build_token (openssl rand -hex 24) and gitea_webhook_secret
#   point builds_root / browse_root / registry_path where you want them

cp config/com.local.unitybuild.plist ~/Library/LaunchAgents/
#   edit YOURUSER, the LAN IP, port, and the Unity service-account secrets
launchctl load ~/Library/LaunchAgents/com.local.unitybuild.plist
```

Create a Unity **service account** for unattended CLI auth
(https://docs.unity.com/en-us/cloud/accounts/create-service-account) and put
its ID/secret in the plist.

Open `http://<mini-lan-ip>:8712/` on your phone, paste the build token once,
and start onboarding projects.

## Gitea webhook (per repo, same secret for all)

In each Gitea repo → Settings → Webhooks → Add → Gitea:
- URL: `http://<mini-lan-ip>:8712/webhook/gitea`
- Method POST, Content type `application/json`
- Secret: the `gitea_webhook_secret` from config.json (same for every repo)
- Trigger: Push events

The service routes each push to the matching project by repo URL, so one
endpoint handles all four of your projects.

## Config keys (config.json)

| key                   | meaning                                             |
|-----------------------|-----------------------------------------------------|
| `build_script`        | path to scripts/build.sh                            |
| `log_dir`             | per-build logs                                      |
| `registry_path`       | where the project registry JSON lives               |
| `builds_root`         | where cloned projects go                            |
| `browse_root`         | sandbox root for the "Browse the Mac" picker        |
| `build_token`         | shared secret the phone UI sends                    |
| `gitea_webhook_secret`| HMAC secret matching the Gitea webhooks             |

## Design decisions / drawbacks

- **Fast path preserved**: the editor is uninstalled/reinstalled only when the
  project's version isn't present (i.e. on a version bump), never on a normal
  build. Per-project toggle: "Uninstall other Unity versions to save space".
- **git sync** is `fetch` → `reset --hard origin/<branch>` → `clean -fd`,
  keeping `Library/` so builds stay fast. Per-project "git clean -fdx" toggle
  for a pristine tree.
- **iOS is two stages**: Unity emits an Xcode project, `xcodebuild` compiles and
  signs, `xcrun devicectl` installs to the device (Xcode 15+).
- **Security**: bind to the LAN IP, token on all mutating endpoints, HMAC on the
  webhook, the directory browser is sandboxed to `browse_root`, deletes only
  ever touch clones under `builds_root`, and no request input is passed through
  a shell string.
- **Must be logged in**: signing needs keychain access, so this runs as a
  LaunchAgent (your user); keep the mini logged in and awake.
- **Unity CLI is experimental/beta** — if `unity editors -i --json` output
  shifts, adjust the small python parsers in build.sh.

## Files

```
app/server.py        FastAPI app: projects API, browser, builds, webhook
app/registry.py      JSON-backed project store
app/onboarding.py    clone / adopt / verify / install BuildScript.cs
app/ui.py            the phone + config web UI
scripts/build.sh     the build pipeline (all inputs via env from the server)
config/config.example.json
config/com.local.unitybuild.plist
```
