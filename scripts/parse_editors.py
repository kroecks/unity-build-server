#!/usr/bin/env python3
"""
Normalize `unity editors -i --json` output into a predictable JSON array of
{"version","path"} objects, tolerating the different shapes the (beta) CLI emits.

Reads raw CLI output on stdin. Writes a JSON array on stdout.
Exit codes:
  0  parsed successfully (array printed, possibly empty)
  2  output was not parseable as the expected structure (prints [] )

Observed real shape (Unity CLI 1.0.0-beta):
  { "success": true, "data": { "items": [ { "version": "...",
      "location": "/Applications/Unity/Hub/Editor/<v>/Unity.app", ... } ] } }
Older/assumed shape (bare list):
  [ { "version": "...", "path": "..." } ]
Both are handled; so is a top-level {"items":[...]}.
"""
import sys, json

raw = sys.stdin.read().strip()
try:
    data = json.loads(raw)
except Exception:
    print("[]"); sys.exit(2)

# Dig out the list of editor records wherever it lives.
items = None
if isinstance(data, list):
    items = data
elif isinstance(data, dict):
    if isinstance(data.get("items"), list):
        items = data["items"]
    elif isinstance(data.get("data"), dict) and isinstance(data["data"].get("items"), list):
        items = data["data"]["items"]

if items is None:
    print("[]"); sys.exit(2)

out = []
for e in items:
    if not isinstance(e, dict):
        continue
    version = e.get("version")
    if not version:
        continue
    # The beta uses "location" for the .app path; older shape used "path".
    path = e.get("location") or e.get("path") or ""
    out.append({"version": version, "path": path})

print(json.dumps(out))
sys.exit(0)
