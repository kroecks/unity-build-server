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
</style>
</head>
<body>
  <h1>Unity build machine</h1>

  <div class="card">
    <label for="token">Build token <small class="hint">(stored on this device only)</small></label>
    <input id="token" type="password" placeholder="token" autocomplete="off">
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
      <button class="full" id="cloneGo">Clone &amp; onboard</button>
      <small class="hint">The service clones it, detects the Unity version, and installs the build script.</small>
    </div>

    <div id="browsePane" style="display:none">
      <label>Pick a project folder on the Mac</label>
      <div class="meta" id="browsePath"></div>
      <div class="card" id="browseList" style="padding:0; max-height:40vh; overflow:auto"></div>
      <label>Project name</label>
      <input id="browseName" type="text" placeholder="My Game">
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
    <pre id="log"></pre>
    <div class="logbar">
      <span id="logfollow">following live output</span>
      <button class="sec" id="jumpLatest" style="display:none">↓ Jump to latest</button>
    </div>
  </div>

  <h2>Recent builds</h2>
  <div id="history"></div>

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

// ---- project list -------------------------------------------------------- //
async function loadProjects() {
  let projs = [];
  try { projs = await (await fetch("/api/projects")).json(); } catch(e){}
  const wrap = $("projects"); wrap.innerHTML = "";
  if (!projs.length) { wrap.innerHTML = '<div class="card meta">No projects yet. Onboard one below.</div>'; return; }
  for (const p of projs) wrap.appendChild(projectCard(p));
}

function projectCard(p) {
  const el = document.createElement("div"); el.className = "card";
  const needs = [];
  if (!p.development_team) needs.push("Team ID");
  if (!p.bundle_id) needs.push("Bundle ID");
  const versionBadge = p.unity_version
    ? `<span class="badge ok">${p.unity_version}</span>` : `<span class="badge warn">no version</span>`;
  const scriptBadge = p.build_script_installed
    ? `<span class="badge ok">build script ✓</span>` : `<span class="badge warn">no build script</span>`;
  el.innerHTML = `
    <div class="proj-head">
      <div class="proj-name">${esc(p.name)}</div>
      <div>${versionBadge} ${scriptBadge}</div>
    </div>
    <div class="meta">${esc(p.repo_url)} · ${esc(p.branch)}</div>
    <div class="meta">${esc(p.local_path)}</div>
    ${needs.length ? `<div class="meta" style="color:#fbbf24">Needs: ${needs.join(", ")} — tap Edit</div>` : ""}
    <div class="btn-row">
      <button data-act="build" ${needs.length?"disabled":""}>Build${needs.length?" (configure first)":""}</button>
      <button class="sec" data-act="edit">Edit</button>
    </div>
    <div class="editArea" style="display:none"></div>`;
  el.querySelector('[data-act="build"]').onclick = () => startBuild(p);
  el.querySelector('[data-act="edit"]').onclick = () => toggleEdit(el, p);
  return el;
}

function toggleEdit(el, p) {
  const area = el.querySelector(".editArea");
  if (area.style.display === "block") { area.style.display = "none"; return; }
  area.style.display = "block";
  area.innerHTML = `
    <label>Apple Team ID</label><input type="text" data-f="development_team" value="${esc(p.development_team)}">
    <label>Bundle ID</label><input type="text" data-f="bundle_id" value="${esc(p.bundle_id)}">
    <label>Device UDID <small class="hint">(optional; blank = build only)</small></label>
    <input type="text" data-f="device_udid" value="${esc(p.device_udid)}">
    <label>Branch <small class="hint">(pushed iOS deltas must live on this branch)</small></label>
    <div class="row">
      <select data-f="branch" id="branchSel-${p.id}"><option value="${esc(p.branch)}">${esc(p.branch)}</option></select>
      <button class="sec" data-s="loadbr" style="flex:0 0 auto">↻ branches</button>
    </div>
    <div class="toggle"><input type="checkbox" data-f="webhook_deploys" ${p.webhook_deploys?"checked":""}><label>Deploy to phone on git push</label></div>
    <div class="toggle"><input type="checkbox" data-f="reclaim_disk" ${p.reclaim_disk?"checked":""}><label>Uninstall other Unity versions to save space</label></div>
    <div class="toggle"><input type="checkbox" data-f="clean_ignored" ${p.clean_ignored?"checked":""}><label>git clean -fdx (nukes Library/, slower builds)</label></div>

    <label>Pre-build steps <small class="hint">(run in order before the build; first failure aborts)</small></label>
    <div id="steps-${p.id}"></div>
    <button class="sec" data-s="addstep" style="margin-top:6px">+ Add step</button>

    <div class="btn-row" style="margin-top:12px">
      <button data-s="save">Save</button>
      <button class="sec" data-s="verify">Re-verify</button>
      <button class="danger" data-s="del">Remove</button>
    </div>`;

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
  const body = { name:$("cloneName").value, repo_url:$("cloneUrl").value, branch:$("cloneBranch").value || "main" };
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
  const body = { name: $("browseName").value, local_path: browseSelected };
  if (!body.name) { alert("Project name required"); return; }
  try { await api("POST","/api/projects/adopt", body); $("addPanel").style.display="none"; await loadProjects(); }
  catch(e){ alert("Onboard failed: " + e.message); }
};

// ---- build + logs -------------------------------------------------------- //
let es = null;
let currentBuildId = null;   // the build currently shown/streaming, for Stop
function setState(s){ const el=$("state"); el.textContent=s; el.className=s.split(" ")[0]; }
function showStop(on){ $("stopBuild").style.display = on ? "inline-block" : "none"; }
async function startBuild(p) {
  $("buildCard").style.display="block";
  $("buildTitle").textContent = "Building: " + p.name;
  $("log").textContent=""; setState("submitting");
  $("buildCard").scrollIntoView({behavior:"smooth"});
  try {
    const b = await api("POST", `/build/${p.id}`);
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
  es = new EventSource(`/logs/${id}`);
  setState("running");
  es.onmessage = e => { pending.push(e.data); scheduleFlush(); };
  es.addEventListener("done", e => {
    flushLog(); setState(e.data); es.close(); showStop(false); stopProgress(); refreshStatus();
  });
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
  if (!confirm("Stop this build? Unity and Xcode will be terminated.")) return;
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
async function viewLog(b) {
  if (es) es.close();
  currentBuildId = null; showStop(false); stopProgress();
  pending = []; liveLines = []; truncatedCount = 0;
  $("fulllogwrap").style.display = "none";   // finished log is loaded whole
  $("buildCard").style.display="block";
  $("buildTitle").textContent = b.project_name + " · " + b.created_iso;
  setState(b.state);
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
  const wrap = $("history"); wrap.innerHTML = "";

  // show the running build (and any queued) at the top
  const active = [];
  if (st.current) active.push(st.current);
  for (const q of st.queued) active.push(q);
  for (const b of active) wrap.appendChild(historyRow(b, true));
  for (const b of st.history) wrap.appendChild(historyRow(b, false));
  if (!wrap.children.length)
    wrap.innerHTML = '<div class="card meta">No builds yet.</div>';

  // Drive the progress bar from the running build, but only while we're
  // actually viewing it (currentBuildId set by stream()). Re-seeds each poll so
  // the elapsed clock stays accurate and the average updates as it's learned.
  if (st.current && st.current.state === "running" && currentBuildId === st.current.id) {
    startProgress(st.current.started_at ? null : null,  // started_at is epoch below
                  st.current.elapsed, st.current.avg_duration, st.current.started_iso);
  } else if (!st.current || st.current.state !== "running") {
    stopProgress();
  }
}
function historyRow(b, active) {
  const el = document.createElement("div");
  el.className = "card"; el.style.padding = "10px 12px"; el.style.cursor = "pointer";
  el.innerHTML = `
    <div class="proj-head">
      <div><span class="${b.state}">${ICON[b.state]||"•"}</span>
           <b>${esc(b.project_name)}</b></div>
      <span class="badge">${esc(b.source)}${b.deploy?" · deploy":""}</span>
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
          <button class="sec" data-a="install">📲 Install</button>` : ""}
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
  if (inst) inst.onclick = async () => {
    try { await api("POST", `/build/${b.project_id}/install/${b.id}`); refreshStatus(); }
    catch(e){ alert("Install failed: " + e.message); }
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
loadProjects();
refreshStatus();
setInterval(refreshStatus, 4000);  // keep the page live without a reload
</script>
</body>
</html>
"""
