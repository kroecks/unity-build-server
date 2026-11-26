"""
Device inventory — every phone / tablet / headset the mini can deploy to.

Discovery is live (nothing to type in):
  Android / Quest   `adb devices -l` for what's attached (USB or WiFi), plus
                    `adb mdns services` for headsets advertising Wireless
                    Debugging on the LAN; paired ones get `adb connect`ed.
  iPhone / iPad     `xcrun devicectl list devices` (paired CoreDevices, USB or
                    network).

Each device gets a STABLE id that survives cable/WiFi/port changes:
  Android  ro.serialno (the USB serial), not the adb transport — a WiFi
           transport is ip:port or an mDNS name, and the port changes on every
           reboot with Wireless Debugging.
  iOS      the hardware UDID (what devicectl / xcodebuild -destination use).

Known devices are persisted (devices.json) with a user nickname, so an offline
headset still shows up in pickers as "offline" instead of vanishing, and
projects can keep it as a default deploy target.

kind is what project targeting filters on:
  "quest"   Meta Quest headsets (Android under the hood, but a separate target)
  "android" every other Android device
  "iphone" / "ipad"
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

PLATFORM_KINDS = {
    "ios": ["iphone", "ipad"],
    "android": ["quest", "android"],
}
KIND_LABEL = {"quest": "Quest", "android": "Android", "iphone": "iPhone", "ipad": "iPad"}

# Fallback hints when the manufacturer can't be read yet (e.g. unauthorized).
# Quest codenames from `adb devices -l`: hollywood (2), seacliff (Pro), eureka (3).
# NOT "panther" (Quest 3S) — that's also the Pixel 7's codename.
_QUEST_HINT = re.compile(r"quest|oculus|hollywood|seacliff|eureka", re.I)
# mDNS instance names look like adb-<serial>-<6 random chars>
_MDNS_SERIAL = re.compile(r"^adb-(.+)-[A-Za-z0-9]{6}$")
ADDRESS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]*:\d{1,5}$")


async def _run(cmd: list[str], timeout: float = 10) -> tuple[int, str]:
    """Run without a shell; (rc, combined output). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
    except (FileNotFoundError, PermissionError, OSError):
        return 127, f"(command not found: {cmd[0]})"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
        return 124, "(timed out)"
    return proc.returncode or 0, (out or b"").decode(errors="replace")


def _is_wifi_transport(t: str) -> bool:
    return ":" in t or "._adb-tls-connect." in t or "._adb._tcp" in t


def classify_android(maker: str, *hints: str) -> str:
    """Quest vs other Android. The manufacturer prop is authoritative (Quests
    report "Oculus"); name hints are only a fallback when it's unknown."""
    if maker:
        return "quest" if re.search(r"oculus|meta", maker, re.I) else "android"
    return "quest" if any(h and _QUEST_HINT.search(h) for h in hints) else "android"


class DeviceStore:
    """Known devices (id -> record) on disk + the latest live scan in memory."""

    def __init__(self, path: Path, adb_path: Callable[[], str]):
        self.path = Path(path)
        self._adb = adb_path
        self._lock = threading.Lock()
        self._scan_lock = asyncio.Lock()
        self._last_scan = 0.0
        self._live: dict[str, dict] = {}     # id -> runtime fields from last scan
        self.last_errors: list[str] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})

    # ---- persistence ----------------------------------------------------- #
    def _read(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    def rename(self, dev_id: str, name: str) -> Optional[dict]:
        with self._lock:
            data = self._read()
            if dev_id not in data:
                return None
            data[dev_id]["name"] = name.strip()[:60] or data[dev_id].get("model", dev_id)
            self._write(data)
        return self.get(dev_id)

    def forget(self, dev_id: str) -> bool:
        with self._lock:
            data = self._read()
            if dev_id not in data:
                return False
            del data[dev_id]
            self._write(data)
        self._live.pop(dev_id, None)
        return True

    # ---- views ------------------------------------------------------------ #
    def _merge(self, rec: dict) -> dict:
        live = self._live.get(rec["id"], {})
        d = dict(rec)
        d.update({"online": False, "state": "not seen", "transport": "",
                  "connection": ""})
        d.update(live)
        d["kind_label"] = KIND_LABEL.get(d.get("kind", ""), d.get("kind", ""))
        return d

    def list(self) -> list[dict]:
        devs = [self._merge(r) for r in self._read().values() if r.get("platform")]
        devs.sort(key=lambda d: (not d["online"], d.get("platform", ""),
                                 d.get("kind", ""), d.get("name", "").lower()))
        return devs

    def get(self, dev_id: str) -> Optional[dict]:
        rec = self._read().get(dev_id)
        return self._merge(rec) if rec else None

    def find(self, ref: str) -> Optional[dict]:
        """Look a device up by id, adb transport, WiFi address or UDID (for the
        legacy single `device_udid` field, which held any of these)."""
        if not ref:
            return None
        d = self.get(ref)
        if d:
            return d
        for d in self.list():
            if ref in (d.get("transport"), d.get("wifi_address"), d.get("udid")):
                return d
        return None

    # ---- scanning --------------------------------------------------------- #
    async def scan(self, max_age: float = 5.0) -> list[dict]:
        """Refresh live state (cached for max_age seconds; 0 = force)."""
        async with self._scan_lock:
            if time.time() - self._last_scan >= max_age:
                errors: list[str] = []
                a, i = await asyncio.gather(self._scan_android(errors),
                                            self._scan_ios(errors))
                found = a + i
                self._apply(found)
                self.last_errors = errors
                self._last_scan = time.time()
        return self.list()

    def _apply(self, found: list[dict]) -> None:
        now = time.time()
        live: dict[str, dict] = {}
        with self._lock:
            data = self._read()
            for f in found:
                rec = data.get(f["id"], {"id": f["id"]})
                rec["platform"] = f["platform"]
                # Don't let a half-known (unauthorized) scan downgrade a known kind.
                if f.get("kind") and not (f.get("state") != "device" and rec.get("kind")):
                    rec["kind"] = f["kind"]
                for k in ("model", "udid", "wifi_address"):
                    if f.get(k):
                        rec[k] = f[k]
                # Default nickname: the device's own name ("Kyle's iPad") if it
                # has one, else the model. Users can rename it in the UI.
                rec.setdefault("name", f.get("name_hint") or f.get("model") or f["id"])
                rec.setdefault("kind", f.get("kind") or "android")
                if f.get("online"):
                    rec["last_seen"] = now
                data[f["id"]] = rec
                live[f["id"]] = {k: f[k] for k in ("online", "state", "transport", "connection")
                                 if k in f}
            self._write(data)
        self._live = live

    async def _scan_android(self, errors: list[str]) -> list[dict]:
        adb = self._adb()
        if adb == "adb" and not shutil.which("adb"):
            return []                      # no adb on this host — not an error
        # 1. mDNS: headsets advertising Wireless Debugging. Connecting only
        #    succeeds if this host was paired with it once (adb pair).
        rc, out = await _run([adb, "mdns", "services"], timeout=8)
        mdns: dict[str, str] = {}          # serial (from instance name) -> ip:port
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and "_adb" in parts[1] and "pairing" not in parts[1]:
                m = _MDNS_SERIAL.match(parts[0])
                if ADDRESS_RE.match(parts[2]):
                    mdns[m.group(1) if m else parts[0]] = parts[2]

        rc, out = await _run([adb, "devices", "-l"], timeout=10)
        if rc == 127:
            return []
        if rc != 0:
            errors.append(f"adb devices failed: {out.strip()[:200]}")
        attached = self._parse_adb_devices(out)

        # 2. Reconnect WiFi devices that aren't attached: fresh mDNS addresses
        #    first, then each known device's last address (legacy tcpip:5555).
        connected_addrs = {t for t in attached if ":" in t}
        known = self._read()
        want = set(mdns.values())
        for rec in known.values():
            if rec.get("platform") == "android" and rec.get("wifi_address"):
                want.add(rec["wifi_address"])
        attached_ids = set()
        for t, info in attached.items():
            m = _MDNS_SERIAL.match(t.split(".", 1)[0])
            if m:
                attached_ids.add(m.group(1))
            elif not _is_wifi_transport(t):
                attached_ids.add(t)
        todo = [a for a in want if a not in connected_addrs
                and not any(mdns.get(s) == a for s in attached_ids)]
        if todo:
            await asyncio.gather(*[_run([adb, "connect", a], timeout=4) for a in todo])
            rc, out = await _run([adb, "devices", "-l"], timeout=10)
            attached = self._parse_adb_devices(out)

        # 3. Identify each ready transport (real serial + maker) in parallel.
        async def ident(t: str, info: dict) -> dict:
            dev = {"platform": "android", "transport": t, "state": info["state"],
                   "online": info["state"] == "device",
                   "connection": "wifi" if _is_wifi_transport(t) else "usb",
                   "model": info.get("model", "").replace("_", " ")}
            serial = maker = ""
            if dev["online"]:
                rc, o = await _run([adb, "-s", t, "shell",
                                    "getprop ro.serialno; getprop ro.product.manufacturer; "
                                    "getprop ro.product.model"], timeout=8)
                lines = [x.strip() for x in o.splitlines()]
                if rc == 0 and len(lines) >= 3:
                    serial, maker = lines[0], lines[1]
                    dev["model"] = lines[2] or dev["model"]
            if not serial:
                m = _MDNS_SERIAL.match(t.split(".", 1)[0])
                serial = m.group(1) if m else ("" if ":" in t else t)
            if not serial:                                # ip:port, never identified
                prev = next((r for r in known.values() if r.get("wifi_address") == t), None)
                serial = prev["id"] if prev else t
            dev["id"] = serial
            dev["kind"] = classify_android(maker, dev["model"], info.get("product", ""),
                                           info.get("device", ""))
            if dev["connection"] == "wifi":
                dev["wifi_address"] = t if ":" in t else mdns.get(serial, "")
            return dev

        devs = await asyncio.gather(*[ident(t, i) for t, i in attached.items()])
        # One device can be attached twice (USB + WiFi). Keep the best transport
        # (ready over not-ready, USB over WiFi) but remember the WiFi address.
        best: dict[str, dict] = {}
        for d in devs:
            cur = best.get(d["id"])
            if cur is None:
                best[d["id"]] = d; continue
            rank = lambda x: (x["online"], x["connection"] == "usb")
            wifi = d.get("wifi_address") or cur.get("wifi_address")
            if rank(d) > rank(cur):
                best[d["id"]] = d
            if wifi:
                best[d["id"]]["wifi_address"] = wifi
        return list(best.values())

    @staticmethod
    def _parse_adb_devices(out: str) -> dict[str, dict]:
        res = {}
        for line in out.splitlines():
            if not line.strip() or line.startswith(("List of", "*", "adb ")):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            info = {"state": parts[1]}
            for p in parts[2:]:
                if ":" in p:
                    k, v = p.split(":", 1)
                    info[k] = v
            res[parts[0]] = info
        return res

    async def _scan_ios(self, errors: list[str]) -> list[dict]:
        if not shutil.which("xcrun"):
            return []
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="devicectl-")
        os.close(fd)
        try:
            rc, out = await _run(["xcrun", "devicectl", "list", "devices",
                                  "--json-output", tmp], timeout=20)
            try:
                data = json.loads(Path(tmp).read_text() or "{}")
            except Exception:
                data = {}
        finally:
            try: os.unlink(tmp)
            except OSError: pass
        if rc != 0 and not data:
            errors.append(f"devicectl failed: {out.strip()[:200]}")
            return []
        res = []
        for d in (data.get("result") or {}).get("devices") or []:
            hw = d.get("hardwareProperties") or {}
            cp = d.get("connectionProperties") or {}
            dp = d.get("deviceProperties") or {}
            dtype = (hw.get("deviceType") or "").lower()
            if dtype not in ("iphone", "ipad"):
                continue                    # watches, TVs, Vision Pro: not targets
            udid = hw.get("udid") or d.get("identifier") or ""
            if not udid:
                continue
            tunnel = (cp.get("tunnelState") or "").lower()
            paired = (cp.get("pairingState") or "paired").lower() == "paired"
            # devicectl lists every paired device, reachable or not; "unavailable"
            # means it can't be reached right now.
            online = paired and tunnel not in ("unavailable", "")
            transport = (cp.get("transportType") or "").lower()
            res.append({
                "id": udid, "udid": udid, "platform": "ios", "kind": dtype,
                "model": hw.get("marketingName") or hw.get("productType") or dtype,
                "name_hint": dp.get("name", ""),
                "online": online,
                "state": "available" if online else (tunnel or "unpaired"),
                "transport": udid,
                "connection": "usb" if transport == "wired" else ("wifi" if transport else ""),
            })
        return res
