"""The phone + config UI, kept in its own module so server.py stays readable."""

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Unity Build</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:16px/1.4 -apple-system,system-ui,sans-serif;
         background:#0f1115; color:#e6e8eb; padding:16px; max-width:720px; margin:0 auto; }
  h1 { font-size:18px; margin:4px 0 14px; }
  h2 { font-size:15px; margin:18px 0 8px; opacity:.85; }
  .card { background:#1a1d24; border:1px solid #2a2f3a; border-radius:12px;
          padding:14px; margin-bottom:12px; }
  .proj-head { display:flex; justify-content:space-between; align-items:center; gap:8px; }
  .proj-name { font-weight:600; font-size:16px; }
  .meta { font-size:12px; opacity:.7; margin-top:4px; word-break:break-all; }
  .badge { font-size:11px; padding:2px 7px; border-radius:20px; background:#2a2f3a; }
  .badge.warn { background:#7c2d12; color:#fed7aa; }
  .badge.ok { background:#14532d; color:#bbf7d0; }
  label { font-size:13px; opacity:.8; display:block; margin:8px 0 3px; }
  input[type=text], input[type=password], select {
      width:100%; background:#05070a; color:#e6e8eb; border:1px solid #2a2f3a;
      border-radius:8px; padding:10px 12px; font-size:15px; }
  .row { display:flex; gap:8px; flex-wrap:wrap; }
  .row > * { flex:1; min-width:120px; }
  .toggle { display:flex; align-items:center; gap:8px; margin-top:10px; }
  .toggle input { width:auto; }
  button { border:0; border-radius:10px; padding:12px 16px; font-size:15px;
           font-weight:600; background:#3b82f6; color:#fff; cursor:pointer; }
  button.sec { background:#2a2f3a; }
  button.danger { background:#7f1d1d; }
  button:disabled { opacity:.5; }
  button.full { width:100%; margin-top:10px; }
  .btn-row { display:flex; gap:8px; margin-top:10px; }
  .btn-row button { flex:1; }
  pre { background:#05070a; border:1px solid #1a1d24; border-radius:10px; padding:12px;
        font-size:12px; line-height:1.35; height:55vh; overflow-y:scroll;
        -webkit-overflow-scrolling:touch; overscroll-behavior:contain;
        white-space:pre-wrap; word-break:break-word; margin-top:12px; }
  .logbar { display:flex; justify-content:space-between; align-items:center;
            margin-top:10px; font-size:12px; opacity:.75; }
  .logbar button { padding:5px 10px; font-size:12px; }
  .progbar { height:8px; background:#2a2f3a; border-radius:6px; overflow:hidden; }
  .progfill { height:100%; width:0%; background:#3b82f6; border-radius:6px;
              transition:width .9s linear; }
  .progfill.over { background:#f59e0b; }   /* past the ETA */
  #state { font-weight:600; margin:10px 0; }
  .queued{color:#f59e0b} .running{color:#3b82f6} .success{color:#22c55e} .failed{color:#ef4444}
  .browse-item { display:flex; justify-content:space-between; align-items:center;
                 padding:10px; border-bottom:1px solid #2a2f3a; cursor:pointer; }
  .browse-item:hover { background:#222733; }
  .tabs { display:flex; gap:6px; margin-bottom:10px; }
  .tabs button { flex:1; background:#2a2f3a; }
  .tabs button.active { background:#3b82f6; }
  small.hint { opacity:.6; font-size:12px; }
  #timing { margin:8px 0; font-size:12px; }
  #timing summary { cursor:pointer; opacity:.85; padding:4px 0; }
  .trow { display:grid; grid-template-columns: 1fr 64px; gap:2px 8px; margin:5px 0 0; }
  .trow .tname { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .trow .tdur { text-align:right; opacity:.8; font-variant-numeric:tabular-nums; }
  .trow .tbar { grid-column:1 / span 2; height:5px; background:#2a2f3a; border-radius:4px; overflow:hidden; }
  .trow .tbar > div { height:100%; background:#3b82f6; }
  .trow.hot .tbar > div { background:#f59e0b; }
  .thead { margin:10px 0 2px; opacity:.6; text-transform:uppercase; letter-spacing:.04em; font-size:11px; }
  /* devices */
  .dev { display:flex; align-items:center; gap:10px; padding:9px 2px; border-bottom:1px solid #222733; }
  .dev:last-child { border-bottom:0; }
  .dev .grow { flex:1; min-width:0; }
  .dev button { padding:5px 9px; font-size:12px; }
  .dot { width:9px; height:9px; border-radius:50%; background:#4b5563; flex:0 0 auto; }
  .dot.on { background:#22c55e; box-shadow:0 0 6px #22c55e88; }
  .dot.warn { background:#f59e0b; }
  .chip { display:inline-flex; align-items:center; gap:5px; font-size:12px; padding:2px 8px;
          border-radius:20px; background:#222733; margin:4px 4px 0 0; }
  .modal { position:fixed; inset:0; background:#000a; display:flex; align-items:flex-end;
           justify-content:center; z-index:50; }
  .modal-box { background:#1a1d24; border:1px solid #2a2f3a; border-radius:14px 14px 0 0;
               width:100%; max-width:720px; max-height:80vh; overflow:auto; padding:14px; }
  @media (min-width:740px){ .modal { align-items:center; } .modal-box { border-radius:14px; } }
  .pick { display:flex; align-items:center; gap:10px; padding:11px 4px; border-bottom:1px solid #222733; cursor:pointer; }
  .pick input { width:20px; height:20px; }
  .pick.off { opacity:.5; cursor:default; }
  .kinds { display:flex; gap:14px; flex-wrap:wrap; }
  .kinds label { display:flex; align-items:center; gap:6px; margin:4px 0; }
  .kinds input { width:auto; }
</style>
</head>
<body>
  <h1>Unity build machine</h1>

  <div id="diskWarn" style="display:none; background:#7c2d12; color:#fed7aa;
       border-radius:10px; padding:10px 12px; margin-bottom:12px; font-size:14px"></div>

  <div class="card">
    <label for="token">Build token <small class="hint">(stored on this device only)</small></label>
    <input id="token" type="password" placeholder="token" autocomplete="off">
  </div>

  <div class="logbar" style="margin-top:4px">
    <h2 style="margin:0">Devices</h2>
    <span>
      <button class="sec" id="toggleAddDev" title="Pair / connect a device over WiFi">+ WiFi device</button>
      <button class="sec" id="scanDevices">↻ Scan</button>
    </span>
  </div>
  <div class="card" id="devices" style="padding:4px 12px"><div class="meta">Scanning…</div></div>
  <div class="card" id="addDevPanel" style="display:none">
    <b>Add a device over WiFi</b>
    <div class="meta" style="margin-top:6px">
      <b>Quest / Android 11+ (Wireless debugging):</b> on the device open Developer options →
      Wireless debugging → <i>Pair device with pairing code</i>. Enter the address and code it
      shows. Pair once; after that it's found automatically on every scan, even after reboots.<br>
      <b>Already in tcpip mode</b> (<code>adb tcpip 5555</code> over USB): enter <code>ip:5555</code>
      and tap Connect.
    </div>
    <div class="row" style="margin-top:6px">
      <div style="flex:2"><label>Address</label><input id="devAddr" type="text" placeholder="192.168.1.42:37123" autocomplete="off"></div>
      <div><label>Pairing code</label><input id="devCode" type="text" inputmode="numeric" placeholder="123456" autocomplete="off"></div>
    </div>
    <div class="btn-row">
      <button id="devPair">Pair</button>
      <button class="sec" id="devConnect">Connect</button>
    </div>
    <div class="meta" id="devOut" style="white-space:pre-wrap"></div>
  </div>

  <div id="projects"></div>

  <button class="full sec" id="toggleAdd">+ Onboard a project</button>

  <div class="card" id="addPanel" style="display:none">
    <h2>Onboard a new project</h2>
    <div class="tabs">
      <button id="tabClone" class="active">Clone from Gitea</button>
      <button id="tabBrowse">Browse the Mac</button>
    </div>

    <div id="clonePane">
      <label>Project name</label>
      <input id="cloneName" type="text" placeholder="My Game">
      <label>Repo URL</label>
      <input id="cloneUrl" type="text" placeholder="http://terronas:3000/etc/game.git">
      <label>Branch</label>
      <input id="cloneBranch" type="text" value="main">
      <label>Platform</label>
      <select id="clonePlatform"><option value="ios">iOS</option><option value="android">Android / Quest</option></select>
      <button class="full" id="cloneGo">Clone &amp; onboard</button>
      <small class="hint">The service clones it, detects the Unity version, and installs the build script.</small>
    </div>

    <div id="browsePane" style="display:none">
      <label>Pick a project folder on the Mac</label>
      <div class="meta" id="browsePath"></div>
      <div class="card" id="browseList" style="padding:0; max-height:40vh; overflow:auto"></div>
      <label>Project name</label>
      <input id="browseName" type="text" placeholder="My Game">
      <label>Platform</label>
      <select id="browsePlatform"><option value="ios">iOS</option><option value="android">Android / Quest</option></select>
      <button class="full" id="browseGo" disabled>Onboard this folder</button>
      <small class="hint">Folders marked ● are Unity projects.</small>
    </div>
  </div>

  <div class="card" id="buildCard" style="display:none">
    <div class="proj-head">
      <div class="proj-name" id="buildTitle"></div>
      <span>
        <button class="danger" id="stopBuild" style="padding:6px 10px; display:none">■ Stop</button>
        <button class="sec" id="closeBuild" style="padding:6px 10px">✕</button>
      </span>
    </div>
    <div id="state">idle</div>
    <div id="progwrap" style="display:none; margin:8px 0">
      <div class="progbar"><div class="progfill" id="progfill"></div></div>
      <div class="logbar"><span id="progtext"></span><span id="progstart" class="hint"></span></div>
    </div>
    <div class="logbar" id="fulllogwrap" style="display:none">
      <span class="hint">showing recent lines only</span>
      <button class="sec" id="fullLog">View full log</button>
    </div>
    <details id="timing" style="display:none"><summary>⏱ Where the time went</summary><div id="timingBody"></div></details>
    <pre id="log"></pre>
    <div class="logbar">
      <span id="logfollow">following live output</span>
      <button class="sec" id="jumpLatest" style="display:none">↓ Jump to latest</button>
    </div>
  </div>

  <h2>Recent builds</h2>
  <div id="history"></div>

  <div class="logbar" style="margin-top:14px">
    <h2 style="margin:0">Storage</h2>
    <span><button class="sec" id="loadDisk">Measure</button></span>
  </div>
  <div id="diskPanel" class="card meta" style="padding:10px 12px">
    Tap “Measure” to see disk free and how much each project’s builds use.
  </div>

  <div class="logbar" style="margin-top:14px">
    <h2 style="margin:0">All builds</h2>
    <span>
      <button class="sec" id="selectMode">Select</button>
      <button class="sec" id="cleanupOld" title="Delete builds older than 7 days">Clean &gt;7d</button>
    </span>
  </div>
  <div id="batchBar" class="logbar" style="display:none; background:#1a1d24; border-radius:8px; padding:8px 10px">
    <span id="selCount">0 selected</span>
    <span>
      <button class="danger" id="batchDelete">Delete selected</button>
      <button class="sec" id="selectCancel">Cancel</button>
    </span>
  </div>
  <div id="allbuilds"></div>
  <button class="sec full" id="loadMore" style="display:none; margin-top:8px">Load more</button>

<div class="modal" id="picker" style="display:none">
  <div class="modal-box">
    <div class="proj-head">
      <b id="pickTitle">Pick devices</b>
      <button class="sec" id="pickClose" style="padding:6px 10px">✕</button>
    </div>
    <div class="meta" id="pickSub"></div>
    <div id="pickList" style="margin-top:6px"></div>
    <div class="btn-row">
      <button class="sec" id="pickRescan" style="flex:0 0 auto">↻</button>
      <button id="pickGo">Install</button>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const token = () => $("token").value;
$("token").value = localStorage.getItem("ubs_token") || "";
$("token").addEventListener("change", e => localStorage.setItem("ubs_token", e.target.value));

const hdr = () => ({ "x-build-token": token(), "content-type": "application/json" });

async function api(method, url, body) {
  const opt = { method, headers: hdr() };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(url, opt);
  if (!r.ok) throw new Error((await r.json().catch(()=>({}))).detail || r.status);
  return r.json();
}

// ---- devices ------------------------------------------------------------- //
// The inventory comes from /api/devices (adb + devicectl scan on the mini).
// Projects target device KINDS (quest / android / iphone / ipad) and keep a
// list of default device ids; Install / Build to… / logs pick per run.
let DEVS = [];
let KIND_LABEL = { quest:"Quest", android:"Android", iphone:"iPhone", ipad:"iPad" };
let PLATFORM_KINDS = { ios:["iphone","ipad"], android:["quest","android"] };
let PROJS = [];
let DEV_ERRORS = [];
let DEVS_LOADED = false;

const devById = id => DEVS.find(d => d.id === id);
const platOf = p => (p.platform || "ios");
function allowedKinds(p) {
  const every = PLATFORM_KINDS[platOf(p)] || [];
  const ks = (p.target_kinds || []).filter(k => every.includes(k));
  return ks.length ? ks : every;
}
const compatible = (p, d) => d.platform === platOf(p) && allowedKinds(p).includes(d.kind);
function defaultIds(p) {
  if ((p.deploy_devices || []).length) return p.deploy_devices;
  if (p.device_udid) {   // legacy single target
    const d = DEVS.find(d => [d.id, d.transport, d.wifi_address, d.udid].includes(p.device_udid));
    return [d ? d.id : p.device_udid];
  }
  return [];
}
function devState(d) {
  if (d.online) return `online · ${d.connection || "?"}`;
  if (d.state === "unauthorized") return "unauthorized — accept the debugging prompt on the device";
  if (d.last_seen) return `offline · seen ${ago(d.last_seen)}`;
  return d.state || "offline";
}
function ago(t) {
  const s = Date.now()/1000 - t;
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s/60) + " min ago";
  if (s < 86400) return Math.round(s/3600) + " h ago";
  return Math.round(s/86400) + " d ago";
}
const dotCls = d => d.online ? "dot on" : (d.state === "unauthorized" ? "dot warn" : "dot");

async function loadDevices(refresh) {
  try {
    const r = await (await fetch("/api/devices" + (refresh ? "?refresh=1" : ""))).json();
    DEVS = r.devices || []; KIND_LABEL = r.kinds || KIND_LABEL;
    PLATFORM_KINDS = r.platform_kinds || PLATFORM_KINDS;
    DEV_ERRORS = r.errors || []; DEVS_LOADED = true;
    renderDevices(); renderAllChips();
  } catch(e) { $("devices").innerHTML = `<div class="meta">Device scan failed: ${esc(e.message)}</div>`; }
}
function renderDevices() {
  if (!DEVS_LOADED) return;
  const wrap = $("devices"); wrap.innerHTML = "";
  if (!DEVS.length) {
    wrap.innerHTML = `<div class="meta" style="padding:8px 0">No devices yet. Plug one in over USB
      (Quest: Developer Mode on, accept “Allow USB debugging”), or add one over WiFi.</div>`;
  }
  for (const d of DEVS) {
    const row = document.createElement("div"); row.className = "dev";
    const users = PROJS.filter(p => defaultIds(p).includes(d.id)).map(p => p.name);
    row.innerHTML = `
      <span class="${dotCls(d)}"></span>
      <div class="grow">
        <div><b>${esc(d.name)}</b> <span class="badge">${esc(d.kind_label || d.kind)}</span></div>
        <div class="meta">${esc(devState(d))}${d.model && d.model !== d.name ? " · " + esc(d.model) : ""}
          · <span title="device id">${esc(d.id)}</span></div>
        ${users.length ? `<div class="meta">default for: ${users.map(esc).join(", ")}</div>` : ""}
      </div>
      <button class="sec" data-a="ren" title="Rename">✎</button>
      <button class="sec" data-a="forget" title="Forget">✕</button>`;
    row.querySelector('[data-a="ren"]').onclick = async () => {
      const n = prompt("Name for this device:", d.name); if (n === null) return;
      try { await api("PATCH", `/api/devices/${encodeURIComponent(d.id)}`, { name:n }); loadDevices(); }
      catch(e){ alert("Rename failed: " + e.message); }
    };
    row.querySelector('[data-a="forget"]').onclick = async () => {
      if (!confirm(`Forget ${d.name}? It's removed from project defaults; it reappears if scanned again.`)) return;
      try { await api("DELETE", `/api/devices/${encodeURIComponent(d.id)}`); await loadProjects(); loadDevices(); }
      catch(e){ alert("Forget failed: " + e.message); }
    };
    wrap.appendChild(row);
  }
  for (const e of DEV_ERRORS) {
    const m = document.createElement("div"); m.className = "meta"; m.style.color = "#fbbf24";
    m.textContent = e; wrap.appendChild(m);
  }
}
$("scanDevices").onclick = async () => {
  $("scanDevices").disabled = true; $("scanDevices").textContent = "Scanning…";
  try { await loadDevices(true); }
  finally { $("scanDevices").disabled = false; $("scanDevices").textContent = "↻ Scan"; }
};
$("toggleAddDev").onclick = () => {
  const p = $("addDevPanel"); p.style.display = p.style.display === "none" ? "block" : "none";
};
async function devWifi(kind) {
  const body = { address: $("devAddr").value.trim() };
  if (kind === "pair") body.code = $("devCode").value.trim();
  $("devOut").textContent = kind === "pair" ? "Pairing…" : "Connecting…";
  try {
    const r = await api("POST", `/api/devices/${kind}`, body);
    $("devOut").textContent = (r.ok ? "✓ " : "✕ ") + (r.output || "")
      + (r.ok && kind === "pair" ? "\nPaired. It should appear above within a scan or two." : "");
    loadDevices(true);
  } catch(e) { $("devOut").textContent = "✕ " + e.message; }
}
$("devPair").onclick = () => devWifi("pair");
$("devConnect").onclick = () => devWifi("connect");

// ---- device picker (Install / Build to… / logs) -------------------------- //
// Resolves to an array of device ids, or null if cancelled. multi=false is a
// single choice (logs) and auto-picks when exactly one device is online.
function pickDevices(p, { title, go, multi = true }) {
  return new Promise(async resolve => {
    let list = [], incompatible = 0;
    const fetchList = async (refresh) => {
      $("pickList").innerHTML = '<div class="meta">Scanning…</div>';
      try {
        const r = await (await fetch(`/api/projects/${p.id}/devices${refresh ? "?refresh=1" : ""}`)).json();
        list = r.devices || []; incompatible = r.incompatible_count || 0;
        $("pickSub").textContent = `${p.name} deploys to `
          + (r.allowed_kinds || []).map(k => KIND_LABEL[k] || k).join(" / ")
          + (incompatible ? ` · ${incompatible} other device(s) hidden` : "");
      } catch(e) { list = []; $("pickSub").textContent = "Scan failed: " + e.message; }
    };
    await fetchList(false);
    if (!multi) {
      const on = list.filter(d => d.online);
      if (on.length === 1) { resolve([on[0].id]); return; }
    }
    const checked = new Set(list.filter(d => d.online && d.default).map(d => d.id));
    if (!checked.size && multi) {            // nothing defaulted: preselect if only one online
      const on = list.filter(d => d.online); if (on.length === 1) checked.add(on[0].id);
    }
    const render = () => {
      const wrap = $("pickList"); wrap.innerHTML = "";
      if (!list.length) wrap.innerHTML = '<div class="meta">No compatible devices known. Plug one in or add one over WiFi, then ↻.</div>';
      for (const d of list) {
        const row = document.createElement("label"); row.className = "pick" + (d.online ? "" : " off");
        row.innerHTML = `
          <input type="${multi ? "checkbox" : "radio"}" name="pickdev" ${checked.has(d.id) ? "checked" : ""} ${d.online ? "" : "disabled"}>
          <span class="${dotCls(d)}"></span>
          <div class="grow" style="flex:1"><b>${esc(d.name)}</b> <span class="badge">${esc(d.kind_label)}</span>
            ${d.default ? '<span class="badge ok">default</span>' : ""}
            <div class="meta">${esc(devState(d))}</div></div>`;
        const inp = row.querySelector("input");
        inp.onchange = () => {
          if (!multi) checked.clear();
          inp.checked ? checked.add(d.id) : checked.delete(d.id);
          update();
        };
        wrap.appendChild(row);
      }
      update();
    };
    const update = () => {
      $("pickGo").disabled = !checked.size;
      $("pickGo").textContent = checked.size > 1 ? `${go} (${checked.size})` : go;
    };
    const close = (v) => { $("picker").style.display = "none"; resolve(v); };
    $("pickTitle").textContent = title;
    $("pickClose").onclick = () => close(null);
    $("picker").onclick = (e) => { if (e.target === $("picker")) close(null); };
    $("pickRescan").onclick = async () => { await fetchList(true);
      for (const id of [...checked]) if (!list.some(d => d.id === id && d.online)) checked.delete(id);
      render(); };
    $("pickGo").onclick = () => close([...checked]);
    render();
    $("picker").style.display = "flex";
  });
}

async function installTo(projectId, buildId, btn) {
  const p = PROJS.find(x => x.id === projectId);
  if (!p) { alert("Project no longer exists"); return; }
  const ids = await pickDevices(p, { title: "Install to…", go: "Install" });
  if (!ids || !ids.length) return;
  if (btn) { btn.disabled = true; btn.textContent = "Installing…"; }
  try { await api("POST", `/build/${projectId}/install/${buildId}`, { devices: ids }); refreshStatus(); }
  catch(e){ alert("Install failed: " + e.message); }
  finally { if (btn) { btn.disabled = false; btn.textContent = "📲 Install"; } }
}

// Chips on each project card: its default targets with live status.
function chipsHtml(p) {
  const ids = defaultIds(p);
  const kinds = allowedKinds(p).map(k => KIND_LABEL[k] || k).join(" / ");
  if (!ids.length) return `<span class="meta">Targets ${esc(kinds)} · no default — Build won't install; use “Build to…”</span>`;
  return `<span class="meta">Deploys to:</span> ` + ids.map(id => {
    const d = devById(id);
    return d ? `<span class="chip"><span class="${dotCls(d)}"></span>${esc(d.name)}</span>`
             : `<span class="chip"><span class="dot"></span>${esc(id)}</span>`;
  }).join("");
}
function renderAllChips() {
  document.querySelectorAll("[data-chips]").forEach(el => {
    const p = PROJS.find(x => x.id === el.dataset.chips); if (p) el.innerHTML = chipsHtml(p);
  });
}

// ---- project list -------------------------------------------------------- //
async function loadProjects() {
  let projs = [];
  try { projs = await (await fetch("/api/projects")).json(); } catch(e){}
  PROJS = projs;
  const wrap = $("projects"); wrap.innerHTML = "";
  if (!projs.length) { wrap.innerHTML = '<div class="card meta">No projects yet. Onboard one below.</div>'; return; }
  for (const p of projs) wrap.appendChild(projectCard(p));
  renderDevices();   // refresh the "default for" lines
}

const isAndroid = p => (p.platform || "ios") === "android";
const PLATFORM_LABEL = { ios: "iOS", android: "Android / Quest" };

function projectCard(p) {
  const el = document.createElement("div"); el.className = "card";
  const needs = [];
  // Android needs no Apple bits; signing falls back to the debug keystore.
  if (!isAndroid(p)) {
    if (!p.development_team) needs.push("Team ID");
    if (!p.bundle_id) needs.push("Bundle ID");
  }
  const platBadge = `<span class="badge">${PLATFORM_LABEL[p.platform||"ios"] || esc(p.platform)}</span>`;
  const versionBadge = p.unity_version
    ? `<span class="badge ok">${p.unity_version}</span>` : `<span class="badge warn">no version</span>`;
  const scriptBadge = p.build_script_installed
    ? `<span class="badge ok">build script ✓</span>` : `<span class="badge warn">no build script</span>`;
  el.innerHTML = `
    <div class="proj-head">
      <div class="proj-name">${esc(p.name)}</div>
      <div>${platBadge} ${versionBadge} ${scriptBadge}</div>
    </div>
    <div class="meta">${esc(p.repo_url)} · ${esc(p.branch)}${
      isAndroid(p) ? " · " + (p.android_build_aab ? "AAB" : "APK")
                   + (p.keystore_path ? " · release-signed" : " · debug-signed") : ""}</div>
    <div class="meta">${esc(p.local_path)}</div>
    ${needs.length ? `<div class="meta" style="color:#fbbf24">Needs: ${needs.join(", ")} — tap Edit</div>` : ""}
    <div data-chips="${p.id}" style="margin-top:6px">${chipsHtml(p)}</div>
    <div class="btn-row">
      <button data-act="build" ${needs.length?"disabled":""}>Build${needs.length?" (configure first)":""}</button>
      <button class="sec" data-act="buildto" ${needs.length?"disabled":""} title="Build and install to devices you pick">Build to…</button>
      <button class="sec" data-act="edit">Edit</button>
    </div>
    <div class="btn-row">
      <button class="sec" data-act="devlogs">Device logs (relaunch)</button>
      <button class="sec" data-act="lastlogs">Last run logs</button>
      <button class="sec" data-act="logzip" title="Download all log sources as a zip">⤓ Logs zip</button>
    </div>
    <div class="editArea" style="display:none"></div>`;
  el.querySelector('[data-act="build"]').onclick = () => startBuild(p);
  el.querySelector('[data-act="buildto"]').onclick = async () => {
    const ids = await pickDevices(p, { title: "Build and install to…", go: "Build & install" });
    if (ids && ids.length) startBuild(p, ids);
  };
  el.querySelector('[data-act="edit"]').onclick = () => toggleEdit(el, p);
  // Logs read from ONE device: auto-picked when only one is online.
  const logDev = async () => {
    const ids = await pickDevices(p, { title: "Logs from which device?", go: "Show logs", multi: false });
    return ids && ids.length ? ids[0] : null;
  };
  el.querySelector('[data-act="devlogs"]').onclick = async () => { const d = await logDev(); if (d) streamDeviceLogs(p, d); };
  el.querySelector('[data-act="lastlogs"]').onclick = async () => { const d = await logDev(); if (d) streamLastRunLogs(p, d); };
  el.querySelector('[data-act="logzip"]').onclick = async () => {
    const d = await logDev(); if (d) location.href = `/device-logs-zip/${p.id}?device=${encodeURIComponent(d)}`;
  };
  return el;
}

function toggleEdit(el, p) {
  const area = el.querySelector(".editArea");
  if (area.style.display === "block") { area.style.display = "none"; return; }
  area.style.display = "block";
  const plat = p.platform || "ios";
  area.innerHTML = `
    <label>Platform <small class="hint">(one project per platform — add a second project on the same repo for the other)</small></label>
    <select data-f="platform">
      <option value="ios" ${plat==="ios"?"selected":""}>iOS</option>
      <option value="android" ${plat==="android"?"selected":""}>Android / Quest</option>
    </select>
    <div data-only="ios">
      <label>Apple Team ID</label><input type="text" data-f="development_team" value="${esc(p.development_team)}">
    </div>
    <label><span data-only="ios">Bundle ID</span><span data-only="android">Package name <small class="hint">(applicationId, e.g. com.terro.game — used to launch &amp; read logs)</small></span></label>
    <input type="text" data-f="bundle_id" value="${esc(p.bundle_id)}">
    <label>Device kinds <small class="hint">(what this project may install onto — e.g. only Quest, so it never lands on a phone)</small></label>
    <div class="kinds" data-kinds></div>
    <label>Default deploy targets <small class="hint">(Build and git-push deploys install to each one that's online; none = build only)</small></label>
    <div data-targets></div>
    <div data-only="android">
      <div class="toggle"><input type="checkbox" data-f="android_build_aab" ${p.android_build_aab?"checked":""}><label>Build AAB (Play Store) instead of APK — AABs can't be adb-installed</label></div>
      <div class="row">
        <div><label>Keystore path <small class="hint">(on the Mac; blank = debug key)</small></label>
          <input type="text" data-f="keystore_path" value="${esc(p.keystore_path)}" placeholder="~/keys/game.keystore"></div>
        <div><label>Key alias</label><input type="text" data-f="keystore_alias" value="${esc(p.keystore_alias)}"></div>
      </div>
      <small class="hint">Passwords are NOT stored here — put them in config.json under "android_keystores".</small>
    </div>
    <label>Branch <small class="hint">(pushed platform deltas must live on this branch)</small></label>
    <div class="row">
      <select data-f="branch" id="branchSel-${p.id}"><option value="${esc(p.branch)}">${esc(p.branch)}</option></select>
      <button class="sec" data-s="loadbr" style="flex:0 0 auto">↻ branches</button>
    </div>
    <div class="toggle"><input type="checkbox" data-f="webhook_deploys" ${p.webhook_deploys?"checked":""}><label>Deploy to the default targets on git push</label></div>
    <div class="toggle"><input type="checkbox" data-f="reclaim_disk" ${p.reclaim_disk?"checked":""}><label>Uninstall other Unity versions to save space</label></div>
    <div class="toggle"><input type="checkbox" data-f="clean_ignored" ${p.clean_ignored?"checked":""}><label>git clean -fdx (nukes Library/, slower builds)</label></div>

    <label style="margin-top:14px">Content / CCD <small class="hint">(per-project; auth is shared on the host)</small></label>
    <div class="row">
      <div><label>CCD project id</label><input type="text" data-f="ccd_project_id" value="${esc(p.ccd_project_id)}"></div>
      <div><label>Environment</label><input type="text" data-f="ccd_environment" value="${esc(p.ccd_environment)}" placeholder="production"></div>
    </div>
    <div class="row">
      <div><label>CCD bucket</label><input type="text" data-f="ccd_bucket" value="${esc(p.ccd_bucket)}"></div>
      <div><label>Addressables profile</label><input type="text" data-f="addressables_profile" value="${esc(p.addressables_profile)}" placeholder="Development"></div>
    </div>
    <label>Remote build path <small class="hint">(where Addressables writes bundles, e.g. ServerData/iOS or ServerData/Android)</small></label>
    <input type="text" data-f="remote_build_path" value="${esc(p.remote_build_path)}" placeholder="${plat==="android"?"ServerData/Android":"ServerData/iOS"}">
    <label>Scripting defines <small class="hint">(semicolon-separated, e.g. TK_IOS_DEV / TK_ANDROID_DEV / TK_QUEST_DEV — needed for CCD auth)</small></label>
    <input type="text" data-f="scripting_defines" value="${esc(p.scripting_defines)}" placeholder="${plat==="android"?"TK_ANDROID_DEV":"TK_IOS_DEV"}">
    <label>Auto-merge from <small class="hint">(merge this branch into the build branch first; e.g. main → ios / android. Conflict aborts.)</small></label>
    <input type="text" data-f="merge_from" value="${esc(p.merge_from)}" placeholder="main">
    <div class="toggle" data-only="ios"><input type="checkbox" data-f="auto_distribute" ${p.auto_distribute?"checked":""}><label>Auto-upload successful webhook builds to TestFlight</label></div>

    <label>Pre-build steps <small class="hint">(run in order before the build; first failure aborts)</small></label>
    <div id="steps-${p.id}"></div>
    <button class="sec" data-s="addstep" style="margin-top:6px">+ Add step</button>

    <div class="btn-row" style="margin-top:12px">
      <button data-s="save">Save</button>
      <button class="sec" data-s="verify">Re-verify</button>
      <button class="danger" data-s="del">Remove</button>
    </div>`;

  // ---- platform-specific sections: show only the selected platform's fields.
  // Hidden fields still save (they keep their values), so switching back and
  // forth never loses e.g. the Team ID.
  const platSel = area.querySelector('[data-f="platform"]');
  const applyPlat = () => area.querySelectorAll("[data-only]").forEach(n => {
    n.style.display = n.dataset.only === platSel.value ? "" : "none";
  });
  // ---- device targeting: kinds for the selected platform, then the known
  // devices of those kinds as default-target checkboxes.
  let kinds = new Set((p.target_kinds || []).length ? p.target_kinds : (PLATFORM_KINDS[plat] || []));
  const targets = new Set(defaultIds(p).filter(id => devById(id)));
  const legacy = defaultIds(p).find(id => !devById(id));   // device_udid not seen by a scan yet
  const renderTargeting = () => {
    const pl = platSel.value, every = PLATFORM_KINDS[pl] || [];
    if (![...kinds].some(k => every.includes(k))) kinds = new Set(every);
    const kw = area.querySelector("[data-kinds]"); kw.innerHTML = "";
    for (const k of every) {
      const l = document.createElement("label");
      l.innerHTML = `<input type="checkbox" ${kinds.has(k) ? "checked" : ""}> ${esc(KIND_LABEL[k] || k)}`;
      l.querySelector("input").onchange = (e) => {
        e.target.checked ? kinds.add(k) : kinds.delete(k);
        if (![...kinds].some(x => every.includes(x))) { kinds.add(k); e.target.checked = true; }  // keep ≥1
        renderTargeting();
      };
      kw.appendChild(l);
    }
    const tw = area.querySelector("[data-targets]"); tw.innerHTML = "";
    const devs = DEVS.filter(d => d.platform === pl && kinds.has(d.kind));
    for (const id of [...targets]) if (!devs.some(d => d.id === id)) targets.delete(id);
    if (!devs.length) tw.innerHTML = '<div class="meta">No matching devices known yet — connect one and ↻ Scan.</div>';
    for (const d of devs) {
      const l = document.createElement("label"); l.className = "pick";
      l.innerHTML = `<input type="checkbox" ${targets.has(d.id) ? "checked" : ""}>
        <span class="${dotCls(d)}"></span><div><b>${esc(d.name)}</b> <span class="badge">${esc(d.kind_label)}</span>
        <div class="meta">${esc(devState(d))}</div></div>`;
      l.querySelector("input").onchange = (e) => { e.target.checked ? targets.add(d.id) : targets.delete(d.id); };
      tw.appendChild(l);
    }
    if (legacy && pl === plat) {
      const m = document.createElement("div"); m.className = "meta"; m.style.color = "#fbbf24";
      m.textContent = `Legacy target “${legacy}” hasn't shown up in a scan yet; it's kept until it does.`;
      tw.appendChild(m);
    }
  };
  platSel.onchange = () => { applyPlat(); renderTargeting(); }; applyPlat(); renderTargeting();
  area._getTargeting = () => {
    if (!DEVS_LOADED) return {};   // inventory not loaded yet: don't touch targets
    const every = PLATFORM_KINDS[platSel.value] || [];
    const tk = every.filter(k => kinds.has(k));
    return {
      target_kinds: tk.length === every.length ? [] : tk,   // all kinds = [] (follows the platform)
      deploy_devices: [...targets],
      // The picker replaces the legacy single field once its device is known.
      ...(legacy ? {} : { device_udid: "" }),
    };
  };

  // ---- pre-build steps editor ----
  const stepsWrap = area.querySelector(`#steps-${p.id}`);
  let steps = JSON.parse(JSON.stringify(p.pre_build_steps || []));
  function renderSteps() {
    stepsWrap.innerHTML = "";
    if (!steps.length) { stepsWrap.innerHTML = '<div class="meta">No pre-build steps.</div>'; }
    steps.forEach((s, idx) => {
      const row = document.createElement("div");
      row.className = "card"; row.style.padding = "8px"; row.style.margin = "6px 0";
      row.innerHTML = `
        <div class="row">
          <input type="text" placeholder="name" value="${esc(s.name)}" data-k="name" style="flex:2">
          <select data-k="type" style="flex:0 0 130px">
            <option value="executeMethod" ${s.type==="executeMethod"?"selected":""}>executeMethod</option>
            <option value="shell" ${s.type==="shell"?"selected":""}>shell</option>
          </select>
        </div>
        <input type="text" placeholder="${s.type==='shell'?'shell command':'Namespace.Class.Method'}" value="${esc(s.value)}" data-k="value" style="margin-top:4px">
        <div class="logbar">
          <label style="margin:0"><input type="checkbox" data-k="enabled" ${s.enabled?"checked":""}> enabled</label>
          <span>
            <button class="sec" data-m="up" ${idx===0?"disabled":""}>↑</button>
            <button class="sec" data-m="down" ${idx===steps.length-1?"disabled":""}>↓</button>
            <button class="danger" data-m="rm">✕</button>
          </span>
        </div>`;
      row.querySelectorAll("[data-k]").forEach(inp => {
        inp.onchange = () => {
          s[inp.dataset.k] = inp.type === "checkbox" ? inp.checked : inp.value;
          if (inp.dataset.k === "type") renderSteps();
        };
      });
      row.querySelector('[data-m="up"]').onclick = () => { [steps[idx-1],steps[idx]]=[steps[idx],steps[idx-1]]; renderSteps(); };
      row.querySelector('[data-m="down"]').onclick = () => { [steps[idx+1],steps[idx]]=[steps[idx],steps[idx+1]]; renderSteps(); };
      row.querySelector('[data-m="rm"]').onclick = () => { steps.splice(idx,1); renderSteps(); };
      stepsWrap.appendChild(row);
    });
  }
  renderSteps();
  area.querySelector('[data-s="addstep"]').onclick = () => {
    steps.push({name:"", type:"executeMethod", value:"", enabled:true}); renderSteps();
  };
  area._getSteps = () => steps;  // save handler reads this

  // ---- branch loader ----
  area.querySelector('[data-s="loadbr"]').onclick = async () => {
    const sel = area.querySelector(`#branchSel-${p.id}`);
    sel.innerHTML = '<option>loading…</option>';
    try {
      const r = await api("GET", `/api/projects/${p.id}/branches`);
      const cur = p.branch;
      const names = r.branches && r.branches.length ? r.branches : [cur];
      sel.innerHTML = "";
      names.forEach(n => { const o=document.createElement("option"); o.value=n; o.textContent=n; if(n===cur)o.selected=true; sel.appendChild(o); });
      if (!names.includes(cur)) { const o=document.createElement("option"); o.value=cur; o.textContent=cur+" (current)"; o.selected=true; sel.insertBefore(o, sel.firstChild); }
      if (r.error) { const o=document.createElement("option"); o.disabled=true; o.textContent="(ls-remote: "+r.error+")"; sel.appendChild(o); }
    } catch(e){ sel.innerHTML = `<option value="${esc(p.branch)}">${esc(p.branch)}</option>`; alert("Branch list failed: " + e.message); }
  };
  area.querySelector('[data-s="save"]').onclick = async () => {
    const fields = {};
    area.querySelectorAll("[data-f]").forEach(i => {
      fields[i.dataset.f] = i.type === "checkbox" ? i.checked : i.value;
    });
    fields.pre_build_steps = area._getSteps();  // from the steps editor
    Object.assign(fields, area._getTargeting());
    try { await api("PATCH", `/api/projects/${p.id}`, fields); await loadProjects(); }
    catch(e){ alert("Save failed: " + e.message); }
  };
  area.querySelector('[data-s="verify"]').onclick = async () => {
    try { await api("POST", `/api/projects/${p.id}/verify`); await loadProjects(); }
    catch(e){ alert("Verify failed: " + e.message); }
  };
  area.querySelector('[data-s="del"]').onclick = async () => {
    if (!confirm("Remove this project from the service? (clone files are kept)")) return;
    try { await api("DELETE", `/api/projects/${p.id}`); await loadProjects(); }
    catch(e){ alert("Remove failed: " + e.message); }
  };
}

// ---- onboarding ---------------------------------------------------------- //
$("toggleAdd").onclick = () => {
  const p = $("addPanel");
  p.style.display = p.style.display === "none" ? "block" : "none";
};
$("tabClone").onclick = () => switchTab(true);
$("tabBrowse").onclick = () => { switchTab(false); browse(""); };
function switchTab(clone) {
  $("tabClone").classList.toggle("active", clone);
  $("tabBrowse").classList.toggle("active", !clone);
  $("clonePane").style.display = clone ? "block" : "none";
  $("browsePane").style.display = clone ? "none" : "block";
}

$("cloneGo").onclick = async () => {
  const body = { name:$("cloneName").value, repo_url:$("cloneUrl").value, branch:$("cloneBranch").value || "main", platform:$("clonePlatform").value };
  if (!body.name || !body.repo_url) { alert("Name and repo URL required"); return; }
  $("cloneGo").disabled = true; $("cloneGo").textContent = "Cloning…";
  try { await api("POST","/api/projects/clone", body); $("addPanel").style.display="none"; await loadProjects(); }
  catch(e){ alert("Onboard failed: " + e.message); }
  finally { $("cloneGo").disabled=false; $("cloneGo").textContent="Clone & onboard"; }
};

let browseSelected = null;
async function browse(path) {
  let data;
  try { data = await api("GET", `/api/browse?path=${encodeURIComponent(path)}`); }
  catch(e){ $("browseList").innerHTML = `<div class="browse-item">${e.message}</div>`; return; }
  $("browsePath").textContent = data.path;
  const list = $("browseList"); list.innerHTML = "";
  if (data.parent !== null) {
    const up = document.createElement("div"); up.className="browse-item";
    up.innerHTML = "<span>⬆ ..</span>"; up.onclick = () => browse(data.parent);
    list.appendChild(up);
  }
  for (const e of data.entries) {
    const item = document.createElement("div"); item.className="browse-item";
    item.innerHTML = `<span>${e.is_unity_project?"● ":"📁 "}${esc(e.name)}</span>`;
    const pick = document.createElement("button"); pick.className="sec"; pick.textContent="Select";
    pick.onclick = (ev) => { ev.stopPropagation(); browseSelected = e.path;
      $("browseName").value = $("browseName").value || e.name;
      $("browseGo").disabled = false; $("browsePath").textContent = "Selected: " + e.path; };
    const open = document.createElement("span"); open.textContent = e.is_unity_project ? "" : "›";
    item.appendChild(pick);
    item.onclick = () => browse(e.path);
    list.appendChild(item);
  }
}
$("browseGo").onclick = async () => {
  if (!browseSelected) return;
  const body = { name: $("browseName").value, local_path: browseSelected, platform: $("browsePlatform").value };
  if (!body.name) { alert("Project name required"); return; }
  try { await api("POST","/api/projects/adopt", body); $("addPanel").style.display="none"; await loadProjects(); }
  catch(e){ alert("Onboard failed: " + e.message); }
};

// ---- build + logs -------------------------------------------------------- //
let es = null;
let currentBuildId = null;   // the build currently shown/streaming, for Stop
function setState(s){ const el=$("state"); el.textContent=s; el.className=s.split(" ")[0]; }
function showStop(on){ $("stopBuild").style.display = on ? "inline-block" : "none"; }
async function startBuild(p, deviceIds) {
  $("buildCard").style.display="block";
  $("buildTitle").textContent = "Building: " + p.name;
  $("log").textContent=""; setState("submitting");
  $("buildCard").scrollIntoView({behavior:"smooth"});
  try {
    const b = await api("POST", `/build/${p.id}`, deviceIds ? { devices: deviceIds } : undefined);
    setState(b.state); stream(b.id);
  } catch(e){ setState("failed: " + e.message); }
}
// Live view keeps only the most recent lines so the DOM text node never grows
// unbounded (a huge node renders as blank gaps when you scroll a backgrounded
// tab). The COMPLETE log is always on disk; "View full log" loads it verbatim.
const LIVE_MAX_LINES = 1200;
let liveLines = [];     // ring of recent lines for the current stream
let pending = [];       // lines arrived since the last paint
let flushScheduled = false;
let truncatedCount = 0; // how many older lines we dropped from the live view

function scheduleFlush() {
  if (flushScheduled) return;
  flushScheduled = true;
  // requestAnimationFrame coalesces bursts; a backgrounded tab fires it on
  // return, so we catch up in one paint instead of thousands of appends.
  requestAnimationFrame(flushLog);
}
function flushLog() {
  flushScheduled = false;
  if (!pending.length) return;
  const log = $("log");
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  liveLines.push(...pending); pending = [];
  if (liveLines.length > LIVE_MAX_LINES) {
    truncatedCount += liveLines.length - LIVE_MAX_LINES;
    liveLines = liveLines.slice(-LIVE_MAX_LINES);
  }
  const header = truncatedCount
    ? `… ${truncatedCount} earlier lines not shown — tap “View full log” below …\n\n` : "";
  log.textContent = header + liveLines.join("\n") + "\n";
  $("fulllogwrap").style.display = truncatedCount ? "block" : "none";
  if (userFollowing && nearBottom) log.scrollTop = log.scrollHeight;
}

function stream(id) {
  if (es) es.close();
  currentBuildId = id; showStop(true);
  liveLines = []; pending = []; truncatedCount = 0;
  $("log").textContent = ""; $("fulllogwrap").style.display = "none";
  $("timing").style.display = "none"; timingFor = id;
  es = new EventSource(`/logs/${id}`);
  setState("running");
  es.onmessage = e => { pending.push(e.data); scheduleFlush(); };
  es.addEventListener("done", e => {
    flushLog(); setState(e.data); es.close(); showStop(false); stopProgress(); refreshStatus();
    loadTiming(id);
  });
  es.onerror = () => {};
}

// Stream the device console into the log viewer (for runtime/addressables errors).
function streamDeviceLogs(p, dev) {
  if (es) es.close();
  currentBuildId = null; showStop(false); stopProgress();
  liveLines = []; pending = []; truncatedCount = 0;
  $("fulllogwrap").style.display = "none"; $("timing").style.display = "none"; timingFor = null;
  $("buildCard").style.display = "block";
  $("buildTitle").textContent = "Device logs: " + p.name + (dev && devById(dev) ? " · " + devById(dev).name : "");
  $("log").textContent = "Launching the app on the phone with console attached… (unlock the phone)\n";
  setState("running");
  $("buildCard").scrollIntoView({behavior:"smooth"});
  es = new EventSource(`/device-logs/${p.id}?device=${encodeURIComponent(dev || "")}`);
  es.onmessage = e => { pending.push(e.data); scheduleFlush(); };
  es.addEventListener("done", e => { flushLog(); setState("idle"); es.close(); });
  es.onerror = () => {};
}

// Pull the LAST run's logs off the device WITHOUT relaunching the app, so a
// crash or failure from a previous run is recoverable. Hits /device-logs-last,
// which collects the unified log + any crash reports. Does not start the app.
function streamLastRunLogs(p, dev) {
  if (es) es.close();
  currentBuildId = null; showStop(false); stopProgress();
  liveLines = []; pending = []; truncatedCount = 0;
  $("fulllogwrap").style.display = "none"; $("timing").style.display = "none"; timingFor = null;
  $("buildCard").style.display = "block";
  $("buildTitle").textContent = "Last run logs: " + p.name + (dev && devById(dev) ? " · " + devById(dev).name : "");
  $("log").textContent = "Pulling the last run's logs from the phone (no relaunch)… "
    + "this can take ~15-30s while the unified log is collected. Keep the phone unlocked.\n";
  setState("running");
  $("buildCard").scrollIntoView({behavior:"smooth"});
  es = new EventSource(`/device-logs-last/${p.id}?device=${encodeURIComponent(dev || "")}`);
  es.onmessage = e => { pending.push(e.data); scheduleFlush(); };
  es.addEventListener("done", e => { flushLog(); setState("idle"); es.close(); });
  es.onerror = () => {};
}

// ---- elapsed timer / ETA / progress bar --------------------------------- //
// Seeded from the server each status poll; ticks locally every second so it's
// smooth. Bar fills elapsed/avg; past the ETA it turns amber and keeps counting.
let progTimer = null, progStartMs = 0, progAvg = null, progForId = null;
function fmtDur(s){
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s/60), sec = s%60;
  return m ? `${m}m ${sec}s` : `${sec}s`;
}
function startProgress(_unused, elapsedSec, avgSec, startedIso){
  progAvg = (avgSec && avgSec > 0) ? avgSec : null;  // avg can refine each poll
  // Seed the start time only once per build so the local clock doesn't jump on
  // every poll; use the server's elapsed (avoids phone/mini clock skew).
  if (progForId !== currentBuildId) {
    progForId = currentBuildId;
    progStartMs = Date.now() - (elapsedSec||0)*1000;
    if (progTimer) clearInterval(progTimer);
    tickProgress();
    progTimer = setInterval(tickProgress, 1000);
  }
  $("progwrap").style.display = "block";
  $("progstart").textContent = startedIso ? ("started " + startedIso) : "";
}
function tickProgress(){
  const elapsed = (Date.now() - progStartMs)/1000;
  const fill = $("progfill"), txt = $("progtext");
  if (progAvg){
    const pct = Math.min(100, (elapsed/progAvg)*100);
    fill.style.width = pct + "%";
    fill.classList.toggle("over", elapsed > progAvg);
    txt.textContent = elapsed > progAvg
      ? `${fmtDur(elapsed)} — over the ~${fmtDur(progAvg)} average`
      : `${fmtDur(elapsed)} of ~${fmtDur(progAvg)} avg (${Math.round(pct)}%)`;
  } else {
    // No history yet — no ETA, just a live elapsed clock and an indeterminate bar.
    fill.style.width = "100%"; fill.classList.remove("over");
    txt.textContent = `${fmtDur(elapsed)} elapsed · no average yet`;
  }
}
function stopProgress(){
  if (progTimer){ clearInterval(progTimer); progTimer = null; }
  progForId = null;
  $("progwrap").style.display = "none";
}
// "View full log" loads the complete on-disk log for the current build.
async function loadFullLog() {
  if (!currentBuildId) return;
  try {
    const r = await fetch(`/logs/${currentBuildId}/raw`);
    if (r.ok) {
      $("log").textContent = await r.text();
      truncatedCount = 0; $("fulllogwrap").style.display = "none";
      $("log").scrollTop = $("log").scrollHeight;
    }
  } catch(e){ alert("Could not load full log: " + e.message); }
}
$("stopBuild").onclick = async () => {
  if (!currentBuildId) return;
  if (!confirm("Stop this build? Unity (and Xcode/adb) will be terminated.")) return;
  $("stopBuild").disabled = true;
  try { await api("POST", `/cancel/${currentBuildId}`); setState("cancelling"); }
  catch(e){ alert("Stop failed: " + e.message); }
  finally { $("stopBuild").disabled = false; }
};

// Track whether the user wants to follow the tail. Scrolling up turns it off;
// scrolling back to the bottom (or tapping Jump to latest) turns it back on.
let userFollowing = true;
(function wireLogScroll(){
  const log = $("log");
  log.addEventListener("scroll", () => {
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    userFollowing = atBottom;
    $("jumpLatest").style.display = atBottom ? "none" : "inline-block";
    $("logfollow").textContent = atBottom ? "following live output" : "scrolled up — paused";
  });
})();
$("jumpLatest").onclick = () => {
  const log = $("log"); log.scrollTop = log.scrollHeight;
  userFollowing = true; $("jumpLatest").style.display="none";
  $("logfollow").textContent = "following live output";
};
$("fullLog").onclick = loadFullLog;
$("closeBuild").onclick = () => { if (es) es.close(); $("buildCard").style.display="none"; };

// Load a finished build's saved log (tapped from history), no live stream.
// ---- timing breakdown ------------------------------------------------------ //
// Phases come from the build script's ">>>" headers, timestamped live by the
// server; the Unity section is parsed from Unity's own asset-pipeline summary.
let timingFor = null;
async function loadTiming(id) {
  timingFor = id;
  let t;
  try { const r = await fetch(`/api/builds/${id}/timing`); if (!r.ok) throw 0; t = await r.json(); }
  catch(e){ if (timingFor === id) $("timing").style.display = "none"; return; }
  if (timingFor !== id) return;   // user moved on to another build
  renderTiming(t);
}
function timingRows(rows, total, nameKey, secKey) {
  const max = Math.max(1, ...rows.map(r => r[secKey]));
  return rows.map(r => {
    const sec = r[secKey], pct = total ? sec/total*100 : 0;
    return `<div class="trow${pct >= 25 ? " hot" : ""}" title="${esc(r[nameKey])}">
      <span class="tname">${esc(r[nameKey])}</span>
      <span class="tdur">${fmtDur(sec)}${total ? ` · ${Math.round(pct)}%` : ""}</span>
      <div class="tbar"><div style="width:${(sec/max*100).toFixed(1)}%"></div></div></div>`;
  }).join("");
}
function renderTiming(t) {
  const box = $("timing"), body = $("timingBody");
  const total = t.total || 0;
  // Fold sub-second phases (one-line status messages) so the list stays readable.
  const phases = (t.phases||[]).filter(p => p.duration >= 1);
  const unity = t.unity || [];
  if (!phases.length && !unity.length) { box.style.display = "none"; return; }
  let html = "";
  if (phases.length) {
    html += `<div class="thead">Build steps (in order)</div>` + timingRows(phases, total, "name", "duration");
  } else {
    html += `<div class="meta">No step timings for this build (recorded for builds started after this update).</div>`;
  }
  if (unity.length) {
    html += `<div class="thead">Inside Unity: asset pipeline</div>` + timingRows(unity, total, "name", "seconds");
  }
  body.innerHTML = html;
  box.style.display = "block";
}

async function viewLog(b) {
  if (es) es.close();
  currentBuildId = null; showStop(false); stopProgress();
  pending = []; liveLines = []; truncatedCount = 0;
  $("fulllogwrap").style.display = "none";   // finished log is loaded whole
  $("buildCard").style.display="block";
  $("buildTitle").textContent = b.project_name + " · " + b.created_iso;
  setState(b.state);
  loadTiming(b.id);
  $("buildCard").scrollIntoView({behavior:"smooth"});
  try {
    const r = await fetch(`/logs/${b.id}/raw`);
    $("log").textContent = r.ok ? await r.text() : "(log not found — it may have been rotated away)";
  } catch(e){ $("log").textContent = "(could not load log)"; }
}

// ---- status / history panel --------------------------------------------- //
const ICON = { success:"✓", failed:"✕", running:"●", queued:"…",
               interrupted:"⚠", cancelled:"⊘" };
async function refreshStatus() {
  let st;
  try { st = await (await fetch("/status")).json(); } catch(e){ return; }
  // Disk-space banner: warn before writes start failing.
  const dw = $("diskWarn");
  if (st.disk_free_gb != null && st.disk_low) {
    dw.style.display = "block";
    dw.textContent = `⚠ Low disk on the build machine: ${st.disk_free_gb} GB free. `
      + `Builds and saves may fail. Delete old build outputs under output_root.`;
  } else { dw.style.display = "none"; }
  const wrap = $("history"); wrap.innerHTML = "";

  // #history shows ONLY the live running/queued builds; finished builds live in
  // the paginated "All builds" list below (so nothing is hidden by a 10 cap).
  const active = [];
  if (st.current) active.push(st.current);
  for (const q of st.queued) active.push(q);
  for (const b of active) wrap.appendChild(historyRow(b, true));
  if (!wrap.children.length)
    wrap.innerHTML = '<div class="card meta">No active builds.</div>';
  // Keep the full list fresh too (cheap; shows newly-finished builds).
  if (!selectModeOn) loadAllBuilds(true);

  // Drive the progress bar from the running build, but only while we're
  // actually viewing it (currentBuildId set by stream()). Re-seeds each poll so
  // the elapsed clock stays accurate and the average updates as it's learned.
  if (st.current && st.current.state === "running" && currentBuildId === st.current.id) {
    startProgress(st.current.started_at ? null : null,  // started_at is epoch below
                  st.current.elapsed, st.current.avg_duration, st.current.started_iso);
    loadTiming(st.current.id);   // live breakdown, refreshed each poll
  } else if (!st.current || st.current.state !== "running") {
    stopProgress();
  }
}

// ---- paginated "All builds" list + batch delete ------------------------- //
let allOffset = 0, allTotal = 0, allLoaded = [];
let selectModeOn = false;
const selected = new Set();
const PAGE = 20;

async function loadAllBuilds(reset) {
  if (reset) { allOffset = 0; allLoaded = []; }
  let data;
  try { data = await api("GET", `/api/builds?offset=${allOffset}&limit=${PAGE}`); }
  catch(e){ return; }
  allTotal = data.total;
  if (reset) allLoaded = data.builds;
  else allLoaded = allLoaded.concat(data.builds);
  renderAllBuilds();
}
function renderAllBuilds() {
  const wrap = $("allbuilds"); wrap.innerHTML = "";
  if (!allLoaded.length) { wrap.innerHTML = '<div class="card meta">No builds yet.</div>'; }
  for (const b of allLoaded) wrap.appendChild(allBuildRow(b));
  $("loadMore").style.display = allLoaded.length < allTotal ? "block" : "none";
  $("loadMore").textContent = `Load more (${allLoaded.length} of ${allTotal})`;
  $("selCount").textContent = `${selected.size} selected`;
}
function allBuildRow(b) {
  const el = document.createElement("div");
  el.className = "card"; el.style.padding = "9px 12px";
  const when = esc(b.created_iso||""); const dur = b.elapsed!=null ? " · "+fmtDur(b.elapsed) : "";
  const rc = (b.returncode!=null && b.state!=='success') ? " (rc="+b.returncode+")" : "";
  // Action buttons on finished builds, so Install/Distribute/Download work
  // straight from the list without opening the log. Hidden in select mode
  // (where the whole row is a checkbox target) and while a build is in flight.
  // Show for any SUCCESSFUL build (e.g. build ok but install failed while the
  // phone was away) — the server returns a clean error if the artifact is gone.
  // Download is gated on has_artifact since there must be a file to fetch.
  const showActions = !selectModeOn && b.state === 'success';
  const actions = showActions ? `
    <div class="row" style="margin-top:8px;gap:6px">
      ${b.has_artifact ? `<a href="${b.artifact_url}"><button class="sec">⤓ Download</button></a>` : ""}
      <button class="sec" data-a="install">📲 Install</button>
      ${b.platform === "android" ? "" : '<button class="sec" data-a="distribute">🚀 Distribute</button>'}
      <button class="sec" data-a="tag" title="Name / tag this build">🏷 Tag</button>
    </div>` : "";
  // Version + commit line (only when we have them). version_label is "1.4.2 (57)".
  const idbits = [];
  if (b.version_label) idbits.push(`<b style="color:#cbd5e1">${esc(b.version_label)}</b>`);
  if (b.commit_short) idbits.push(`<span style="color:#93c5fd">${esc(b.commit_short)}</span>`);
  const idline = idbits.length ? `<div class="meta">${idbits.join(" · ")}</div>` : "";
  const labelline = b.label ? `<div class="meta" style="color:#fbbf24">🏷 ${esc(b.label)}</div>` : "";
  el.innerHTML = `
    <div class="proj-head">
      <div style="display:flex;align-items:center;gap:8px">
        ${selectModeOn ? `<input type="checkbox" class="selbox" ${selected.has(b.id)?"checked":""}>` : ""}
        <span class="${b.state}">${ICON[b.state]||"•"}</span>
        <b>${esc(b.project_name)}</b>
      </div>
      <span class="badge">${esc(b.source)}</span>
    </div>
    ${idline}${labelline}
    <div class="meta">${when}${dur} · ${esc(b.state)}${rc}${
      b.deploy && (b.device_names||[]).length ? " · → " + esc(b.device_names.join(", ")) : ""}</div>${actions}`;
  if (selectModeOn) {
    const cb = el.querySelector(".selbox");
    cb.onclick = (ev) => { ev.stopPropagation();
      if (cb.checked) selected.add(b.id); else selected.delete(b.id);
      $("selCount").textContent = `${selected.size} selected`; };
    el.onclick = () => { cb.checked = !cb.checked; cb.onclick({stopPropagation(){}}); };
  } else {
    el.style.cursor = "pointer";
    el.onclick = () => viewLog(b);
    if (showActions) {
      // Stop button clicks from also opening the log view.
      const inst = el.querySelector('[data-a="install"]');
      inst.onclick = (ev) => { ev.stopPropagation(); installTo(b.project_id, b.id, inst); };
      const dist = el.querySelector('[data-a="distribute"]');
      if (dist) dist.onclick = async (ev) => { ev.stopPropagation();
        if (!confirm("Upload this build to TestFlight / App Store Connect?")) return;
        dist.disabled = true; dist.textContent = "Uploading…";
        try { await api("POST", `/build/${b.project_id}/distribute/${b.id}`); refreshStatus(); }
        catch(e){ alert("Distribute failed: " + e.message); }
        finally { dist.disabled = false; dist.textContent = "🚀 Distribute"; }
      };
      const tag = el.querySelector('[data-a="tag"]');
      if (tag) tag.onclick = async (ev) => { ev.stopPropagation();
        const name = prompt("Name / tag for this build:", b.label || "");
        if (name === null) return;   // cancelled
        try {
          await api("PATCH", `/api/builds/${b.id}`, { label: name });
          b.label = name; renderAllBuilds();
        } catch(e){ alert("Couldn't save tag: " + e.message); }
      };
      // The download link shouldn't bubble up to the row's viewLog handler either.
      const dl = el.querySelector("a");
      if (dl) dl.onclick = (ev) => ev.stopPropagation();
    }
  }
  return el;
}
$("loadMore").onclick = () => { allOffset += PAGE; loadAllBuilds(false); };

// ---- storage panel: disk free + per-project build sizes ----------------- //
function gb(n){ return (n==null) ? "?" : n.toFixed(n<10?2:1)+" GB"; }
function bar(pct){
  const c = pct>=90 ? "#ef4444" : pct>=75 ? "#fbbf24" : "#34d399";
  return `<div style="height:8px;background:#2a2f3a;border-radius:4px;overflow:hidden;margin:6px 0">
    <div style="height:100%;width:${Math.min(100,pct||0)}%;background:${c}"></div></div>`;
}
$("loadDisk").onclick = async () => {
  const panel = $("diskPanel");
  panel.innerHTML = "Measuring… (walking build directories, a few seconds)";
  let d;
  try { d = await api("GET", "/api/disk"); }
  catch(e){ panel.innerHTML = "Couldn't measure: " + esc(e.message); return; }
  const v = d.volume || {};
  let html = "";
  if (v.total_gb != null) {
    html += `<div><b>Volume</b> <span class="meta">${esc(v.path||"")}</span></div>`;
    html += bar(v.used_pct);
    html += `<div class="meta">${gb(v.free_gb)} free of ${gb(v.total_gb)} `
         +  `· ${gb(v.used_gb)} used (${v.used_pct}%)</div>`;
  } else {
    html += `<div class="meta">Volume stats unavailable: ${esc(v.error||"")}</div>`;
  }
  html += `<div style="margin-top:10px"><b>Build output</b> — `
       +  `${gb(d.builds_total_gb)} across ${d.project_count} project(s)</div>`;
  for (const p of (d.projects||[])) {
    const share = v.total_gb ? Math.round(p.bytes/ (v.total_gb*1e9) *100) : 0;
    html += `<div style="margin-top:8px;border-top:1px solid #222;padding-top:8px">
      <div style="display:flex;justify-content:space-between">
        <b>${esc(p.project_name)}</b><span>${gb(p.gb)} · ${p.build_count} build(s)</span>
      </div>
      <div class="meta">biggest builds:</div>`;
    for (const b of (p.builds||[]).slice(0,3)) {
      html += `<div class="meta">&nbsp;&nbsp;${esc(b.build_id).slice(0,8)} — `
           +  `${b.mb} MB · ${esc(b.mtime_iso)}</div>`;
    }
    html += `</div>`;
  }
  if (!(d.projects||[]).length) html += `<div class="meta">No build output on disk yet.</div>`;
  panel.innerHTML = html;
};

$("selectMode").onclick = () => {
  selectModeOn = true; selected.clear();
  $("batchBar").style.display = "flex"; renderAllBuilds();
};
$("selectCancel").onclick = () => {
  selectModeOn = false; selected.clear();
  $("batchBar").style.display = "none"; renderAllBuilds();
};
$("batchDelete").onclick = async () => {
  if (!selected.size) return;
  if (!confirm(`Delete ${selected.size} build(s)? Output and logs are removed.`)) return;
  try {
    const r = await api("POST", "/api/builds/delete", { ids: [...selected] });
    alert(`Deleted ${r.removed} of ${r.requested}.`);
    selectModeOn = false; selected.clear(); $("batchBar").style.display = "none";
    loadAllBuilds(true);
  } catch(e){ alert("Batch delete failed: " + e.message); }
};
$("cleanupOld").onclick = async () => {
  if (!confirm("Delete all builds older than 7 days?")) return;
  try {
    const r = await api("POST", "/api/builds/delete", { older_than_days: 7 });
    alert(`Deleted ${r.removed} old build(s).`);
    loadAllBuilds(true);
  } catch(e){ alert("Cleanup failed: " + e.message); }
};

function historyRow(b, active) {
  const el = document.createElement("div");
  el.className = "card"; el.style.padding = "10px 12px"; el.style.cursor = "pointer";
  el.innerHTML = `
    <div class="proj-head">
      <div><span class="${b.state}">${ICON[b.state]||"•"}</span>
           <b>${esc(b.project_name)}</b></div>
      <span class="badge">${esc(b.source)}${b.deploy ? " · → " + esc((b.device_names||[]).join(", ") || "defaults") : ""}</span>
    </div>
    <div class="meta">${esc(b.created_iso||"")} · ${esc(b.state)}${
      b.elapsed!=null ? " · " + fmtDur(b.elapsed) : ""}${
      b.returncode!=null && b.state!=='success' ? " (rc="+b.returncode+")" : ""}</div>
    ${(b.state==='running' || b.state==='queued') ? `
    <div class="logbar" data-noclick="1">
      <span></span>
      <button class="danger" data-a="stop">■ Stop</button>
    </div>` : (b.has_artifact || (b.state!=='running' && b.state!=='queued')) ? `
    <div class="logbar" data-noclick="1">
      <span>${b.has_artifact ? "artifact ready" : ""}</span>
      <span>
        ${b.has_artifact ? `<a href="${b.artifact_url}"><button class="sec">⤓ Download</button></a>
          <button class="sec" data-a="install">📲 Install</button>
          ${b.platform === "android" ? "" : '<button class="sec" data-a="distribute">🚀 Distribute</button>'}` : ""}
        <button class="danger" data-a="del">✕</button>
      </span>
    </div>` : ""}`;
  // Clicking the row opens the log, but not when tapping the action bar.
  el.onclick = (ev) => {
    if (ev.target.closest("[data-noclick]")) return;
    active && b.state==='running' ? reattach(b) : viewLog(b);
  };
  const stop = el.querySelector('[data-a="stop"]');
  if (stop) stop.onclick = async () => {
    if (!confirm("Stop this build?")) return;
    try { await api("POST", `/cancel/${b.id}`); refreshStatus(); }
    catch(e){ alert("Stop failed: " + e.message); }
  };
  const inst = el.querySelector('[data-a="install"]');
  if (inst) inst.onclick = () => installTo(b.project_id, b.id, inst);
  const dist = el.querySelector('[data-a="distribute"]');
  if (dist) dist.onclick = async () => {
    if (!confirm("Export this build and upload it to TestFlight?")) return;
    try { await api("POST", `/build/${b.project_id}/distribute/${b.id}`); refreshStatus(); }
    catch(e){ alert("Distribute failed: " + e.message); }
  };
  const del = el.querySelector('[data-a="del"]');
  if (del) del.onclick = async () => {
    if (!confirm("Delete this build and its output?")) return;
    try { await api("DELETE", `/api/builds/${b.id}`); refreshStatus(); }
    catch(e){ alert("Delete failed: " + e.message); }
  };
  return el;
}
function reattach(b) {
  $("buildCard").style.display="block";
  $("buildTitle").textContent = "Building: " + b.project_name;
  $("log").textContent=""; $("buildCard").scrollIntoView({behavior:"smooth"});
  stream(b.id);
}

function esc(s){ return (s||"").replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
loadProjects().then(() => loadDevices());
refreshStatus();
setInterval(refreshStatus, 4000);  // keep the page live without a reload
setInterval(() => { if ($("picker").style.display === "none") loadDevices(); }, 15000);
</script>
</body>
</html>
"""