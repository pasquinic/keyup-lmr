/* KeyUp — central management UI (vanilla JS, no build step) */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let TOKEN = localStorage.getItem("lmr_token") || "";
const cache = {};
let liveWs = null, dashTimer = null, liveCalls = {}, eventLog = [];

// ------------------------------------------------------------------ api
async function api(path, opts = {}) {
  const r = await fetch("/api" + path, {
    method: opts.method || "GET",
    headers: {"Content-Type": "application/json", Authorization: "Bearer " + TOKEN},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (r.status === 401) { logout(); throw new Error("session expired"); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(typeof data === "object" ? (Array.isArray(data.detail) ? data.detail.map(d => d.loc.slice(-1) + ": " + d.msg).join("; ") : data.detail) : data);
  return data;
}
function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.className = "toast", 3200);
}
const pill = (v, cls) => `<span class="pill ${esc(cls ?? v)}">${esc(String(v).replace("_", " "))}</span>`;
const yes = b => pill(b ? "yes" : "no");
const ago = iso => { if (!iso) return "—"; const s = (Date.now() - new Date(iso)) / 1000; return s < 60 ? `${s|0}s ago` : s < 3600 ? `${s/60|0}m ago` : s < 86400 ? `${s/3600|0}h ago` : new Date(iso).toLocaleString(); };
const dur = s => { s = Math.max(0, s|0); return `${(s/60|0)}:${String(s%60).padStart(2,"0")}`; };
const mhz = v => v ? Number(v).toFixed(5) : "—";

// ------------------------------------------------------------------ auth
function logout() { TOKEN = ""; localStorage.removeItem("lmr_token"); liveWs?.close(); $("#shell").classList.add("hidden"); $("#login").classList.remove("hidden"); }
$("#loginForm").onsubmit = async e => {
  e.preventDefault(); const f = new FormData(e.target);
  try {
    const r = await fetch("/api/auth/login", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(Object.fromEntries(f))});
    const d = await r.json(); if (!r.ok) throw new Error(d.detail);
    TOKEN = d.token; localStorage.setItem("lmr_token", TOKEN); start();
  } catch (err) { $("#loginErr").textContent = err.message; }
};
$("#logout").onclick = logout;
$("#menuBtn").onclick = () => $("#nav").classList.toggle("open");

// ------------------------------------------------------------------ form builder
async function options(src) {
  if (Array.isArray(src)) return src.map(o => typeof o === "object" ? o : {value: o, label: o});
  cache[src] = await api("/" + src);
  return cache[src].map(o => ({value: o.id, label: `${o.id} — ${o.name ?? o.alias ?? o.label ?? ""}`}));
}
async function formModal(title, fields, values = {}, submitLabel = "Save", onSubmit = null) {
  const m = $("#modal"), f = $("#modalForm");
  let html = `<h2>${esc(title)}</h2><div class="formgrid">`;
  let group = null;
  for (const fd of fields) {
    if (fd.group !== group) { if (group) html += `</fieldset>`; group = fd.group; if (group) html += `<fieldset><legend>${esc(group)}</legend>`; }
    const v = values[fd.name] ?? fd.default ?? "";
    const cls = fd.wide ? ' class="wide"' : "";
    if (fd.type === "bool") { html += `<label class="check"${cls}><input type="checkbox" name="${fd.name}" ${v ? "checked" : ""}> ${esc(fd.label)}</label>`; continue; }
    if (fd.type === "select" || fd.type === "multi") {
      const opts = await options(fd.options);
      const sel = fd.type === "multi" ? new Set((v || []).map(String)) : new Set([String(v)]);
      html += `<label${cls}>${esc(fd.label)}<select name="${fd.name}" ${fd.type === "multi" ? "multiple" : ""}>` +
        (fd.nullable ? `<option value="">— none —</option>` : "") +
        opts.map(o => `<option value="${esc(o.value)}" ${sel.has(String(o.value)) ? "selected" : ""}>${esc(o.label)}</option>`).join("") + `</select></label>`;
      continue;
    }
    if (fd.type === "textarea") { html += `<label${cls}>${esc(fd.label)}<textarea name="${fd.name}" rows="3">${esc(v)}</textarea></label>`; continue; }
    html += `<label${cls}>${esc(fd.label)}<input name="${fd.name}" type="${fd.type === "number" ? "number" : "text"}" step="any" value="${esc(v)}" ${fd.required ? "required" : ""} ${fd.readonly && values[fd.name] !== undefined ? "readonly" : ""} placeholder="${esc(fd.placeholder ?? "")}"></label>`;
  }
  if (group) html += `</fieldset>`;
  html += `</div><p class="err" id="formErr"></p><div class="formfoot"><button class="btn ghost" value="cancel" formnovalidate>Cancel</button><button class="btn primary" value="ok">${esc(submitLabel)}</button></div>`;
  f.innerHTML = html; m.showModal();
  const collect = () => {
    const out = {};
    for (const fd of fields) {
      const el = f.elements[fd.name];
      if (fd.type === "bool") out[fd.name] = el.checked;
      else if (fd.type === "multi") out[fd.name] = [...el.selectedOptions].map(o => Number(o.value));
      else if (fd.type === "number") out[fd.name] = el.value === "" ? (fd.nullable ? null : 0) : Number(el.value);
      else if (fd.type === "select") out[fd.name] = el.value === "" ? null : (fd.numeric ? Number(el.value) : el.value);
      else out[fd.name] = el.value;
    }
    return out;
  };
  return new Promise(resolve => {
    f.onsubmit = async e => {
      if (e.submitter?.value !== "ok") { m.close(); return resolve(null); }
      e.preventDefault();
      const data = collect();
      if (onSubmit) {
        try { await onSubmit(data); } catch (err) { $("#formErr").textContent = err.message; return; }
      }
      m.close(); resolve(data);
    };
  });
}
async function editLoop(title, fields, values, save) {
  const d = await formModal(title, fields, values, "Save", save);
  if (d) toast("Saved");
  return !!d;
}

// ------------------------------------------------------------------ generic resource page
function resourcePage({path, listPath, title, columns, fields, idField = "id", newDefaults = {}, extraToolbar = "", rowActions}) {
  return async el => {
    const rows = await api("/" + (listPath || path));
    el.innerHTML = `<div class="toolbar"><input id="filter" placeholder="Filter…"><span class="spacer"></span>${extraToolbar}<button class="btn primary" id="addBtn">+ Add ${esc(title)}</button></div>
      <div class="card tablewrap"><table><thead><tr>${columns.map(c => `<th>${esc(c.label)}</th>`).join("")}<th></th></tr></thead><tbody></tbody></table></div>`;
    const tbody = $("tbody", el);
    const draw = q => {
      const list = rows.filter(r => !q || JSON.stringify(r).toLowerCase().includes(q));
      tbody.innerHTML = list.length ? list.map(r => `<tr>${columns.map(c => `<td>${c.render ? c.render(r) : esc(r[c.key])}</td>`).join("")}
        <td class="actions">${rowActions ? rowActions(r) : ""}<button class="btn sm" data-edit="${esc(r[idField])}">Edit</button> <button class="btn sm danger" data-del="${esc(r[idField])}">Delete</button></td></tr>`).join("")
        : `<tr><td colspan="${columns.length + 1}" class="empty">Nothing here yet</td></tr>`;
    };
    draw("");
    $("#filter", el).oninput = e => draw(e.target.value.toLowerCase());
    $("#addBtn", el).onclick = async () => { if (await editLoop(`New ${title}`, fields, newDefaults, d => api("/" + path, {method: "POST", body: d}))) route(); };
    tbody.onclick = async e => {
      const id = e.target.dataset.edit ?? e.target.dataset.del;
      if (id === undefined) return;
      const row = rows.find(r => String(r[idField]) === id);
      if (e.target.dataset.edit !== undefined) {
        if (await editLoop(`Edit ${title} ${id}`, fields, row, d => api(`/${path}/${id}`, {method: "PUT", body: d}))) route();
      } else if (confirm(`Delete ${title} ${id}?`)) {
        try { await api(`/${path}/${id}`, {method: "DELETE"}); toast("Deleted"); route(); } catch (err) { toast(err.message, true); }
      }
    };
  };
}

const MODES = [{value: "P25P1", label: "P25 Phase I"}, {value: "P25P2", label: "P25 Phase II"}, {value: "DMR", label: "DMR"}, {value: "NXDN", label: "NXDN / NEXEDGE"}, {value: "ANALOG", label: "Analog FM"}];

// ------------------------------------------------------------------ pages
const pages = {};

pages.dashboard = async el => {
  const d = await api("/dashboard");
  $("#sysName").textContent = `${d.system.system_name} · WACN ${d.system.wacn} · SYS ${d.system.system_id}`;
  const online = d.sites.filter(s => s.status === "online").length, st = d.sites.filter(s => s.status === "site_trunking" || s.edge_mode === "site_trunking").length;
  const maxH = Math.max(1, ...Object.values(d.hourly)), maxT = Math.max(1, ...d.top_talkgroups.map(t => t.calls));
  liveCalls = Object.fromEntries(d.call_processing.active_calls.map(c => [c.call_id, c]));
  el.innerHTML = `
  ${d.dvm && d.system.dvm_license_acknowledged !== "true" ? `<div class="card" style="border-color:var(--warn);margin-bottom:16px"><b style="color:var(--warn)">DVM not started.</b> Acknowledge the DVM license terms on the <a href="#/dvm">DVM Network</a> page.</div>` : ""}
  <div class="grid kpis">
    ${kpi(`${online}/${d.sites.length}`, "Sites online")}
    ${d.dvm ? kpi(d.dvm.reachable ? `${d.dvm.peers.length}` : pill("down", "offline"), "FNE peers") : ""}
    ${d.poc ? kpi(d.poc.sessions.length, "PoC phones online") : ""}
    ${kpi(`<span id="kActive">${d.call_processing.active_calls.length}</span>`, "Active calls")}
    ${kpi(d.calls_24h, "Calls · 24h")}
    ${kpi(d.denied_24h, "Denied / busy · 24h")}
    ${kpi(dur(d.airtime_24h_s), "Airtime · 24h")}
    ${kpi(st, "Sites in site trunking")}
    ${kpi(d.counts.subscribers, "Subscriber radios")}
    ${kpi(d.counts.devices_pending, "Devices pending")}
  </div>
  <div class="grid two" style="margin-top:16px">
    <div class="card"><h3><span class="tx"></span>Live call processing</h3><div id="liveCalls"></div></div>
    <div class="card"><h3>Event feed</h3><div class="log" id="evlog"></div></div>
  </div>
  <div class="card" style="margin-top:16px"><h3>Sites</h3><div class="sites">${d.sites.map(siteCard).join("") || '<div class="empty">No sites configured</div>'}</div></div>
  <div class="grid three" style="margin-top:16px">
    <div class="card"><h3>Calls per hour (24h)</h3><div class="bars">${Object.entries(d.hourly).map(([h, n]) => `<div style="height:${n / maxH * 100}%" data-t="${h} · ${n}"></div>`).join("")}</div></div>
    <div class="card"><h3>Busiest talkgroups</h3>${d.top_talkgroups.map(t => `<div class="hbar"><span>${esc(t.name)}</span><span class="b" style="width:${t.calls / maxT * 100}%"></span><span class="mono">${t.calls}</span></div>`).join("") || '<div class="empty">No traffic</div>'}</div>
    ${d.dvm ? `<div class="card"><h3>DVM network</h3><dl class="kv">
      <dt>FNE</dt><dd>${d.dvm.reachable ? pill("online") : pill("offline")}</dd>
      <dt>Peers</dt><dd>${d.dvm.peers.length}</dd><dt>Affiliations</dt><dd>${d.dvm.affiliation_count}</dd>
      <dt>Vocoding</dt><dd>dvmbridge (SIP, analog, cross-mode patches)</dd>
      <dt>Site trunking</dt><dd>native on the air (dvmhost)</dd></dl><p><a href="#/dvm">DVM network details →</a></p></div>` : `<div class="card"><h3>Transcoding</h3><dl class="kv">
      <dt>DVSI vocoders</dt><dd>${d.transcoder.hardware_vocoders.length ? d.transcoder.hardware_vocoders.map(v => esc(v.port + " " + v.product)).join("<br>") : "none (IMBE/AMBE+2 conversions unavailable)"}</dd>
      <dt>Free / leased</dt><dd>${d.transcoder.free} / ${d.transcoder.leased}</dd>
      <dt>Active sessions</dt><dd>${d.transcoder.active_sessions}</dd>
      <dt>Transcoded calls</dt><dd>${d.transcoder.transcoded_calls}</dd>
      <dt>Passthrough</dt><dd>P25P2 ⇄ DMR ⇄ NXDN (AMBE+2)</dd></dl></div>`}
    <div class="card"><h3>Call processing</h3><dl class="kv">
      <dt>Granted</dt><dd>${d.call_processing.calls_granted}</dd><dt>Denied</dt><dd>${d.call_processing.calls_denied}</dd>
      <dt>Frames routed</dt><dd id="kFrames">${d.call_processing.frames_routed}</dd><dt>Frames dropped</dt><dd>${d.call_processing.frames_dropped}</dd>
      <dt>Uptime</dt><dd>${(d.uptime_s / 3600).toFixed(1)} h</dd></dl></div>
    <div class="card"><h3>WireGuard hub</h3><dl class="kv">
      <dt>Mode</dt><dd>${d.wireguard.enabled ? "kernel" : "mock (lab)"}</dd><dt>Interface</dt><dd>${esc(d.wireguard.interface)}</dd>
      <dt>Server IP</dt><dd>${esc(d.wireguard.server_ip)}</dd><dt>Endpoint</dt><dd>${esc(d.wireguard.endpoint)}</dd>
      <dt>Public key</dt><dd>${esc(d.wireguard.public_key)}</dd></dl></div>
    <div class="card"><h3>SIP interconnect</h3><dl class="kv">
      <dt>Gateway</dt><dd>${d.sip.enabled ? (d.sip.connected ? pill("online") : pill("offline")) : "disabled"}</dd>
      <dt>Active legs</dt><dd>${(d.sip.legs || []).length}</dd></dl></div>
  </div>`;
  drawLive(); drawLog();
  clearInterval(dashTimer); dashTimer = setInterval(() => { if (location.hash.startsWith("#/dashboard") || location.hash === "") drawLive(); }, 1000);
};
const kpi = (v, l) => `<div class="card kpi"><div class="v">${v}</div><div class="l">${esc(l)}</div></div>`;
const siteCard = s => {
  const status = s.edge_mode === "site_trunking" ? "site_trunking" : s.status;
  return `<div class="site ${esc(status)}"><div class="n">${esc(s.id)} · ${esc(s.name)}</div>
  <div style="margin-top:6px">${pill(status)} ${s.mode_mix.map(m => pill(m, "mode")).join(" ")}</div>
  <div class="meta">${esc(s.location || "")}<br>${s.channels} channel(s)${s.peers_up !== undefined ? ` · ${s.peers_up} on FNE` : ""} · seen ${ago(s.last_seen)}</div></div>`;
};
function drawLive() {
  const box = $("#liveCalls"); if (!box) return;
  const calls = Object.values(liveCalls);
  if ($("#kActive")) $("#kActive").textContent = calls.length;
  box.innerHTML = calls.length ? calls.map(c => `<div class="callrow ${c.emergency ? "emerg" : ""} ${c.state === "hang" ? "hang" : ""}">
    <span class="tx" style="${c.state === "hang" ? "animation:none;background:var(--warn)" : ""}"></span>
    <div><div class="tg">TG ${esc(c.tg)} · ${esc(c.tg_name)} ${c.emergency ? pill("EMERGENCY", "emerg") : ""} ${c.state === "hang" ? pill("hang") : ""}</div>
    <div class="muted">${esc(c.src_alias)} @ ${esc(c.src_label)} → ${c.dests.map(esc).join(", ") || "no destinations"} · ${esc(c.codec)}${c.via_sip ? " · SIP" : ""}</div></div>
    <div class="t">${dur(Date.now() / 1000 - c.started)}</div></div>`).join("") : `<div class="empty">No active calls</div>`;
}
function drawLog() {
  const box = $("#evlog"); if (!box) return;
  box.innerHTML = eventLog.map(e => `<div><span class="muted">${new Date(e.ts * 1000).toLocaleTimeString()}</span> ${esc(e.text)}</div>`).join("") || `<div class="muted">Waiting for events…</div>`;
}
function logEvent(text, ts) { eventLog.unshift({text, ts: ts || Date.now() / 1000}); eventLog = eventLog.slice(0, 80); drawLog(); }

function connectLive() {
  liveWs?.close();
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/dashboard?token=${encodeURIComponent(TOKEN)}`);
  liveWs = ws;
  ws.onopen = () => { $("#liveDot").className = "live"; $("#liveTxt").textContent = "live"; };
  ws.onclose = () => { $("#liveDot").className = "live off"; $("#liveTxt").textContent = "reconnecting"; if (TOKEN && liveWs === ws) setTimeout(connectLive, 3000); };
  ws.onmessage = m => {
    const e = JSON.parse(m.data);
    if (e.event === "snapshot" || e.event === "tick") { liveCalls = Object.fromEntries(e.call_processing.active_calls.map(c => [c.call_id, c])); drawLive(); if ($("#kFrames")) $("#kFrames").textContent = e.call_processing.frames_routed; return; }
    if (e.event === "call_start" || e.event === "call_hang") { liveCalls[e.call.call_id] = e.call; drawLive(); if (e.event === "call_start") logEvent(`CALL TG ${e.call.tg} (${e.call.tg_name}) from ${e.call.src_alias} @ ${e.call.src_label}${e.call.emergency ? " ⚠ EMERGENCY" : ""}`, e.ts); }
    else if (e.event === "call_end") { delete liveCalls[e.call_id]; drawLive(); logEvent(`END TG ${e.tg} · ${e.duration}s · ${e.reason}`, e.ts); }
    else if (e.event === "call_denied") logEvent(`DENY TG ${e.tg} unit ${e.src || "?"} site ${e.site}: ${e.reason}`, e.ts);
    else if (e.event === "site") { logEvent(`SITE ${e.site_id} ${e.status}${e.detail?.mode ? " (" + e.detail.mode + ")" : ""}`, e.ts); if (location.hash.startsWith("#/dashboard")) clearTimeout(window._sr), window._sr = setTimeout(route, 400); }
    else if (e.event === "device") logEvent(`DEVICE ${e.device_id} ${e.status}`, e.ts);
    else if (e.event === "calls_imported") logEvent(`Imported ${e.count} site-trunking call records from site ${e.site_id}`, e.ts);
    else if (e.event === "sip_leg") logEvent(`SIP ${e.caller} ${e.action} on TG ${e.tg}`, e.ts);
    else if (e.event === "poc") logEvent(`PoC ${e.unit} ${e.alias || ""} ${e.action}${e.platform ? " (" + e.platform + ")" : ""}`, e.ts);
    else if (e.event === "emergency") { logEvent(`${e.on ? "⚠ EMERGENCY" : "Emergency cleared"} TG ${e.tg} unit ${e.unit} ${e.alias || ""}`, e.ts); showAlarm(e); }
  };
}

pages.sites = resourcePage({
  path: "sites", title: "Site",
  columns: [{key: "id", label: "Site"}, {key: "name", label: "Name"}, {key: "location", label: "Location"}, {label: "Status", render: r => pill(r.status)},
    {key: "network_codec", label: "Net codec"}, {label: "Last seen", render: r => ago(r.last_seen)}],
  fields: [{name: "id", label: "Site number", type: "number", required: true, readonly: true}, {name: "name", label: "Name", required: true},
    {name: "location", label: "Location"}, {name: "rfss_id", label: "RFSS ID", type: "number", default: 1},
    {name: "network_codec", label: "Network codec", type: "select", options: ["AUTO", "ULAW", "PCM16", "IMBE", "AMBE2"], default: "AUTO"},
    {name: "callsign", label: "CW ID callsign (blank = off)"}, {name: "latitude", label: "Latitude", type: "number"}, {name: "longitude", label: "Longitude", type: "number"},
    {name: "enabled", label: "Enabled", type: "bool", default: true}, {name: "notes", label: "Notes", type: "textarea", wide: true}],
});

pages.talkgroups = resourcePage({
  path: "talkgroups", title: "Talkgroup",
  columns: [{key: "id", label: "TGID"}, {key: "name", label: "Name"}, {key: "priority", label: "Priority"}, {key: "hang_time_ms", label: "Hang ms"},
    {label: "Emergency", render: r => yes(r.emergency_enabled)}, {label: "Rules", render: r => [r.affiliated_only && "affiliated", r.restricted && "restricted", r.parrot && "parrot", r.site_ids && "sites " + r.site_ids].filter(Boolean).map(esc).join(" · ") || '<span class="muted">open</span>'}, {label: "SIP", render: r => r.allow_sip ? `${pill("yes")} <span class="mono">${esc(r.sip_extension || r.id)}</span>` : yes(false)}, {label: "Enabled", render: r => yes(r.enabled)}],
  fields: [{name: "id", label: "TGID", type: "number", required: true, readonly: true}, {name: "name", label: "Name", required: true},
    {name: "description", label: "Description", wide: true}, {name: "priority", label: "Priority (1-10)", type: "number", default: 5},
    {name: "hang_time_ms", label: "Hang time (ms)", type: "number", default: 3000}, {name: "emergency_enabled", label: "Emergency calls allowed", type: "bool", default: true},
    {name: "allow_sip", label: "Allow SIP interconnect", type: "bool"}, {name: "sip_extension", label: "SIP dial extension", placeholder: "e.g. 7101"},
    {name: "enabled", label: "Enabled", type: "bool", default: true},
    {name: "dmr_slot", label: "DMR timeslot", type: "number", default: 1, group: "DVM talkgroup rules"},
    {name: "affiliated_only", label: "Only repeat where affiliated", type: "bool", group: "DVM talkgroup rules"},
    {name: "restricted", label: "Only subscribers listing this TG may talk", type: "bool", group: "DVM talkgroup rules"},
    {name: "parrot", label: "Parrot (echo test) talkgroup", type: "bool", group: "DVM talkgroup rules"},
    {name: "site_ids", label: "Limit to site numbers (comma list, blank = all)", group: "DVM talkgroup rules"},
    {name: "allow_poc", label: "Selectable by PoC phones", type: "bool", default: true, group: "DVM talkgroup rules"},
    {name: "bridge_mode", label: "Mode for SIP / PoC / analog bridges", type: "select", options: [{value: "P25P1", label: "P25"}, {value: "DMR", label: "DMR"}, {value: "ANALOG", label: "Analog"}], default: "P25P1", group: "DVM talkgroup rules"}],
});

const CHANNEL_FIELDS = [
  {name: "site_id", label: "Site", type: "select", options: "sites", numeric: true, group: "RF"}, {name: "slot", label: "Edge interface port", type: "number", default: 1, group: "RF"},
  {name: "name", label: "Name", required: true, group: "RF"}, {name: "mode", label: "System mode", type: "select", options: MODES, default: "P25P1", group: "RF"},
  {name: "rx_mhz", label: "RX MHz", type: "number", group: "RF"}, {name: "tx_mhz", label: "TX MHz", type: "number", group: "RF"},
  {name: "bandwidth_khz", label: "Bandwidth kHz", type: "number", default: 12.5, group: "RF"}, {name: "role", label: "Role", type: "select", options: ["voice", "control", "data"], default: "voice", group: "RF"},
  {name: "talkgroup_id", label: "Bound talkgroup", type: "select", options: "talkgroups", nullable: true, numeric: true, group: "RF"},
  {name: "nac", label: "P25 NAC (hex)", default: "293", group: "Air interface"}, {name: "color_code", label: "DMR color code", type: "number", default: 1, group: "Air interface"},
  {name: "timeslot", label: "Timeslot (DMR/P25P2)", type: "number", default: 1, group: "Air interface"}, {name: "ran", label: "NXDN RAN", type: "number", default: 1, group: "Air interface"},
  {name: "ctcss_hz", label: "CTCSS Hz (analog)", type: "number", group: "Air interface"}, {name: "default_unit_id", label: "Default unit ID (if radio can't report)", type: "number", group: "Air interface"},
  {name: "audio_device", label: "ALSA device", default: "default", group: "Edge hardware"}, {name: "ptt_gpio", label: "PTT GPIO (BCM)", type: "number", default: 17, group: "Edge hardware"},
  {name: "cor_gpio", label: "COR GPIO (BCM)", type: "number", default: 27, group: "Edge hardware"}, {name: "cor_active_low", label: "COR active low", type: "bool", default: true, group: "Edge hardware"},
  {name: "tx_delay_ms", label: "TX delay ms", type: "number", default: 150, group: "Edge hardware"}, {name: "tx_tail_ms", label: "TX tail ms", type: "number", default: 200, group: "Edge hardware"},
  {name: "rx_gain_db", label: "RX gain dB", type: "number", group: "Edge hardware"}, {name: "tx_gain_db", label: "TX gain dB", type: "number", group: "Edge hardware"},
  {name: "enabled", label: "Enabled", type: "bool", default: true, group: "Edge hardware"},
  {name: "peer_id", label: "FNE peer ID (blank = auto)", type: "number", nullable: true, group: "DVM modem / dvmhost"},
  {name: "modem_type", label: "Modem", type: "select", options: [{value: "uart", label: "UART (DVM-V1 / hotspot / V.24)"}, {value: "null", label: "Null (lab, no RF)"}], default: "uart", group: "DVM modem / dvmhost"},
  {name: "modem_mode", label: "Modem mode", type: "select", options: [{value: "air", label: "Air interface (modem)"}, {value: "dfsi", label: "DFSI (V.24 via DVM-V24)"}], default: "air", group: "DVM modem / dvmhost"},
  {name: "modem_port", label: "Modem / serial port (analog: RTS PTT + CTS COR)", default: "/dev/ttyUSB0", group: "DVM modem / dvmhost"},
  {name: "modem_speed", label: "Serial speed", type: "number", default: 115200, group: "DVM modem / dvmhost"},
  {name: "rx_level", label: "Modem RX level %", type: "number", default: 50, group: "DVM modem / dvmhost"},
  {name: "tx_level", label: "Modem TX level %", type: "number", default: 50, group: "DVM modem / dvmhost"},
  {name: "power_w", label: "TX power (W, informational)", type: "number", default: 10, group: "DVM modem / dvmhost"},
];
pages.channels = resourcePage({
  path: "channels", listPath: "frequencies", title: "Channel",
  columns: [{label: "Site", render: r => `${esc(r.site_id)} · ${esc(r.site_name)}`}, {key: "slot", label: "Port"}, {key: "name", label: "Name"}, {label: "Mode", render: r => pill(r.mode, "mode")},
    {label: "RX / TX MHz", render: r => `<span class="mono">${mhz(r.rx_mhz)} / ${mhz(r.tx_mhz)}</span>`}, {label: "Access", render: r => esc(r.mode.startsWith("P25") ? "NAC " + r.nac : r.mode === "DMR" ? `CC${r.color_code} TS${r.timeslot}` : r.mode === "NXDN" ? "RAN " + r.ran : r.ctcss_hz ? r.ctcss_hz + " Hz" : "CSQ")},
    {label: "Role", render: r => esc(r.role)}, {key: "talkgroup_id", label: "TG"}, {label: "DVM", render: r => `<span class="mono">peer ${esc(r.peer_id ?? "auto")}<br>${r.iden !== null && r.iden !== undefined ? `iden ${r.iden} ch ${r.ch_no}` : "—"}</span>`},
    {label: "Modem", render: r => `<span class="mono">${esc(r.modem_type)} ${esc(r.modem_mode)}<br>${esc(r.modem_port)}</span>`},
    {label: "Checks", render: r => r.warnings.length ? `<div class="warnlist">${r.warnings.map(esc).join("<br>")}</div>` : pill("ok", "yes")}],
  fields: CHANNEL_FIELDS,
});

pages.subscribers = async el => {
  await resourcePage({
    path: "subscribers", title: "Subscriber",
    extraToolbar: `<button class="btn" id="importBtn">Import CSV</button>`,
    columns: [{key: "id", label: "Radio ID"}, {key: "alias", label: "Alias"}, {label: "Mode", render: r => pill(r.mode, "mode")}, {key: "model", label: "Model"},
      {key: "home_site_id", label: "Assigned site"}, {label: "Talkgroups", render: r => r.talkgroup_ids.length ? esc(r.talkgroup_ids.join(", ")) : '<span class="muted">all</span>'},
      {label: "Last seen", render: r => r.last_seen ? `${ago(r.last_seen)} · ${r.last_site_id ? "site " + esc(r.last_site_id) : "PoC/net"} TG ${esc(r.last_tg_id ?? "")}` : "—"}, {label: "Enabled", render: r => yes(r.enabled)},
      {label: "PoC", render: r => r.poc_enabled ? pill("yes") : '<span class="muted">—</span>'}],
    rowActions: r => `<button class="btn sm" data-poc="${esc(r.id)}">PoC QR</button> `,
    fields: [{name: "id", label: "Radio / unit ID", type: "number", required: true, readonly: true}, {name: "alias", label: "Alias", required: true},
      {name: "mode", label: "Mode", type: "select", options: MODES}, {name: "model", label: "Radio model"}, {name: "serial", label: "Serial"},
      {name: "home_site_id", label: "Assigned site / edge device", type: "select", options: "sites", nullable: true, numeric: true},
      {name: "talkgroup_ids", label: "Permitted talkgroups (none = all)", type: "multi", options: "talkgroups", wide: true},
      {name: "enabled", label: "Enabled (inhibit when off)", type: "bool", default: true}, {name: "notify_site_trunking", label: "Send site-trunking data alerts", type: "bool", default: true},
      {name: "poc_enabled", label: "PoC phone allowed for this radio ID", type: "bool", default: false},
      {name: "notes", label: "Notes", type: "textarea", wide: true}],
  })(el);
  el.addEventListener("click", async e => {
    const id = e.target.dataset?.poc;
    if (id) { e.stopPropagation(); await pocActivation(id); }
  }, true);
  $("#importBtn", el).onclick = async () => {
    await formModal("Import subscribers (CSV)", [{name: "csv", label: "id,alias,mode,home_site_id,talkgroups(;-separated)", type: "textarea", wide: true, placeholder: "1001,Engine 1,P25P1,1,100;200"}], {}, "Import", async data => {
      const rows = data.csv.trim().split(/\r?\n/).filter(l => l && !/^id,/i.test(l)).map(l => { const [id, alias, mode, home, tgs] = l.split(",").map(s => s.trim());
        return {id: +id, alias, mode: mode || "P25P1", home_site_id: home ? +home : null, talkgroup_ids: tgs ? tgs.split(";").map(Number) : []}; });
      const r = await api("/subscribers/bulk", {method: "POST", body: rows}); toast(`Imported ${r.imported}`); route();
    });
  };
};

pages.devices = async el => {
  const [devs, toks, sites] = await Promise.all([api("/devices"), api("/enrollment/tokens"), api("/sites")]);
  el.innerHTML = `
  <div class="card"><h3>Edge devices</h3><div class="tablewrap"><table><thead><tr><th>ID</th><th>Site</th><th>Host</th><th>Status</th><th>Link</th><th>Tunnel IP</th><th>WG handshake</th><th>Telemetry</th><th></th></tr></thead><tbody>
  ${devs.map(d => `<tr><td>${d.id}</td><td>${esc(d.site_id)} · ${esc(d.site_name)}</td><td>${esc(d.hostname)}<div class="muted">${esc(d.model)} ${esc(d.serial)} v${esc(d.agent_version)}</div></td>
    <td>${pill(d.status)}</td><td>${d.connected ? pill(d.telemetry.mode === "site_trunking" ? "site_trunking" : "online") : pill("offline")}</td><td class="mono">${esc(d.wg_ip)}</td>
    <td>${d.wg ? `${d.wg.latest_handshake ? ago(new Date(d.wg.latest_handshake * 1000).toISOString()) : "never"}<div class="muted">${esc(d.wg.endpoint)} · ↓${(d.wg.rx_bytes / 1e6).toFixed(1)}MB ↑${(d.wg.tx_bytes / 1e6).toFixed(1)}MB</div>` : '<span class="muted">—</span>'}</td>
    <td class="muted">${d.telemetry.cpu_temp_c ? `${d.telemetry.cpu_temp_c}°C · ` : ""}${d.telemetry.load ? `load ${esc(d.telemetry.load)} · ` : ""}${d.telemetry.vocoders ? esc(d.telemetry.vocoders.join("/")) : ""}${d.telemetry.uptime_s ? ` · up ${(d.telemetry.uptime_s / 3600).toFixed(1)}h` : ""}</td>
    <td class="actions">${d.status === "pending" ? `<button class="btn sm primary" data-a="approve" data-id="${d.id}">Approve</button>` : ""}
      ${d.connected ? `<button class="btn sm" data-a="tone" data-id="${d.id}">Test tone</button>` : ""}
      ${d.status === "active" ? `<button class="btn sm danger" data-a="revoke" data-id="${d.id}">Revoke</button>` : ""}
      <button class="btn sm danger" data-a="delete" data-id="${d.id}">Delete</button></td></tr>`).join("") || `<tr><td colspan="9" class="empty">No devices enrolled yet — create an enrollment token below</td></tr>`}
  </tbody></table></div></div>
  <div class="card" style="margin-top:16px"><h3>Enrollment tokens</h3>
    <div class="toolbar"><select id="tSite">${sites.map(s => `<option value="${s.id}">${s.id} · ${esc(s.name)}</option>`).join("")}</select>
      <input id="tHours" type="number" value="24" style="width:90px" title="valid hours"> <label class="check"><input type="checkbox" id="tAuto"> auto-approve</label>
      <button class="btn primary" id="tNew" ${sites.length ? "" : "disabled"}>Generate token</button></div>
    <div id="tOut"></div>
    <div class="tablewrap"><table><thead><tr><th>Token</th><th>Site</th><th>Auto approve</th><th>Expires</th><th>Used</th><th></th></tr></thead><tbody>
    ${toks.map(t => `<tr><td class="mono">${esc(t.token)}</td><td>${esc(t.site_id)}</td><td>${yes(t.auto_approve)}</td><td>${new Date(t.expires_at).toLocaleString()}</td><td>${t.used_at ? `device ${esc(t.used_by_device)} · ${ago(t.used_at)}` : "—"}</td>
      <td class="actions"><button class="btn sm danger" data-tdel="${esc(t.token)}">Delete</button></td></tr>`).join("") || `<tr><td colspan="6" class="empty">No tokens</td></tr>`}
    </tbody></table></div></div>`;
  el.onclick = async e => {
    const a = e.target.dataset.a, id = e.target.dataset.id;
    try {
      if (a === "approve") await api(`/devices/${id}/approve`, {method: "POST"});
      else if (a === "revoke" && confirm("Revoke device? Its WireGuard peer is removed immediately.")) await api(`/devices/${id}/revoke`, {method: "POST"});
      else if (a === "delete" && confirm("Delete device record?")) await api(`/devices/${id}`, {method: "DELETE"});
      else if (a === "tone") { await api(`/devices/${id}/command`, {method: "POST", body: {command: "test_tone"}}); toast("Test tone sent"); return; }
      else if (e.target.dataset.tdel) await api(`/enrollment/tokens/${e.target.dataset.tdel}`, {method: "DELETE"});
      else if (e.target.id === "tNew") {
        const t = await api("/enrollment/tokens", {method: "POST", body: {site_id: +$("#tSite").value, valid_hours: +$("#tHours").value, auto_approve: $("#tAuto").checked}});
        $("#tOut").innerHTML = `<p>Run on the Raspberry Pi:</p><div class="code">${esc(t.install_command)}</div><br>`; toast("Token created"); return;
      } else return;
      route();
    } catch (err) { toast(err.message, true); }
  };
};

pages.sip = async el => {
  const d = await api("/dashboard");
  await resourcePage({
    path: "sip/routes", title: "SIP Route",
    extraToolbar: `<button class="btn" id="origBtn">Patch call now…</button>`,
    columns: [{key: "name", label: "Name"}, {key: "endpoint", label: "Endpoint"}, {key: "talkgroup_id", label: "TG"}, {key: "ptt_mode", label: "Floor control"},
      {label: "Permanent patch", render: r => yes(r.auto_connect)}, {label: "Enabled", render: r => yes(r.enabled)}],
    fields: [{name: "name", label: "Name", required: true}, {name: "endpoint", label: "Asterisk endpoint", required: true, placeholder: "PJSIP/1001 or PJSIP/5551234@carrier"},
      {name: "talkgroup_id", label: "Talkgroup", type: "select", options: "talkgroups", numeric: true}, {name: "ptt_mode", label: "Phone floor control", type: "select", options: [{value: "dtmf", label: "DTMF (* key / # unkey)"}, {value: "vox", label: "VOX"}]},
      {name: "vox_threshold", label: "VOX threshold (RMS)", type: "number", default: 900}, {name: "auto_connect", label: "Permanent patch (auto dial)", type: "bool"}, {name: "enabled", label: "Enabled", type: "bool", default: true}],
  })(el);
  el.insertAdjacentHTML("afterbegin", `<div class="card" style="margin-bottom:16px"><h3>Gateway</h3>${d.sip.enabled ? `${d.sip.connected ? pill("online") : pill("offline")} Asterisk ARI ·
    ${d.sip.legs.length} active leg(s)${d.sip.legs.map(l => `<div class="muted">${esc(l.caller)} on TG ${esc(l.tg)} ${l.keyed ? pill("TX", "warn") : ""}</div>`).join("")}` : `<span class="muted">Disabled. Set ARI_ENABLED=true and start the asterisk service.</span>`}
    <p class="muted">Inbound: dial a talkgroup's SIP extension. Phones press <b>*</b> to talk and <b>#</b> to release (DTMF mode).</p></div>`);
  $("#origBtn", el).onclick = async () => {
    await editLoop("Patch a SIP endpoint into a talkgroup", [{name: "endpoint", label: "Endpoint", placeholder: "PJSIP/1001", required: true}, {name: "talkgroup_id", label: "Talkgroup", type: "select", options: "talkgroups", numeric: true},
      {name: "ptt_mode", label: "Floor control", type: "select", options: ["dtmf", "vox"]}, {name: "caller_id", label: "Caller ID", default: "LMR"}], {}, b => api("/sip/originate", {method: "POST", body: b}));
  };
};

pages.calls = async el => {
  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  const rows = await api(`/calls?limit=300${q.get("tg") ? "&tg=" + q.get("tg") : ""}${q.get("site") ? "&site=" + q.get("site") : ""}`);
  el.innerHTML = `<div class="toolbar"><input id="fTg" placeholder="TG" value="${esc(q.get("tg") || "")}" style="width:100px"><input id="fSite" placeholder="Site" value="${esc(q.get("site") || "")}" style="width:100px"><button class="btn" id="fGo">Filter</button></div>
  <div class="card tablewrap"><table><thead><tr><th>Start</th><th>TG</th><th>Source</th><th>Site</th><th>Duration</th><th>Destinations</th><th>Codec</th><th>Flags</th><th>Result</th></tr></thead><tbody>
  ${rows.map(c => `<tr><td>${new Date(c.started_at).toLocaleString()}</td><td>${esc(c.talkgroup_id)}</td><td>${esc(c.source_id || "—")}</td><td>${esc(c.source_site_id || (c.via_sip ? "SIP" : "—"))}</td>
    <td class="mono">${dur(c.duration_s)}</td><td class="muted">${esc(c.dest_sites.join(", "))}</td><td>${esc(c.source_codec)}</td>
    <td>${c.emergency ? pill("EMERG", "emerg") : ""} ${c.transcoded ? pill("xcode", "mode") : ""} ${c.site_trunking ? pill("site trunking", "warn") : ""} ${c.via_sip ? pill("sip", "mode") : ""}</td>
    <td>${pill(c.result.split(":")[0], c.result.startsWith("denied") ? "denied" : c.result === "granted" ? "granted" : "warn")} <span class="muted">${esc(c.result.split(":")[1] || "")}</span></td></tr>`).join("") || `<tr><td colspan="9" class="empty">No calls</td></tr>`}
  </tbody></table></div>`;
  $("#fGo", el).onclick = () => location.hash = `#/calls?tg=${$("#fTg").value}&site=${$("#fSite").value}`;
};

pages.settings = async el => {
  const s = await api("/settings");
  const labels = {system_name: "System name", wacn: "WACN (hex)", system_id: "System ID (hex)", require_known_subscriber: "Require known subscriber (true/false)",
    site_trunking_timeout_s: "Site trunking after link loss (s)", site_trunking_announce_interval_s: "Site trunking re-announce interval (s)",
    site_trunking_message: "Site trunking data message", wide_area_message: "Wide-area restored message", default_hang_time_ms: "Default hang time (ms)", max_call_s: "Max call duration (s)",
    call_backend: "Call backend (dvm | native)", dvm_license_acknowledged: "DVM license acknowledged (set on DVM page)", fne_peer_id: "FNE master peer ID",
    fne_address: "FNE address edges dial (tunnel IP)", fne_port: "FNE port", dmr_net_id: "DMR network ID (hex)"};
  el.innerHTML = `<div class="grid two"><div class="card"><h3>System</h3><form id="sForm" class="formgrid">${Object.entries(s).map(([k, v]) => `<label>${esc(labels[k] || k)}<input name="${esc(k)}" value="${esc(v)}"></label>`).join("")}
    <div class="wide"><button class="btn primary">Save &amp; push to sites</button></div></form></div>
    <div class="card"><h3>Change password</h3><form id="pForm" class="formgrid"><label class="wide">Current<input type="password" name="old"></label><label class="wide">New (8+ chars)<input type="password" name="new"></label><div><button class="btn">Update</button></div></form></div></div>`;
  $("#sForm").onsubmit = async e => { e.preventDefault(); try { await api("/settings", {method: "PUT", body: Object.fromEntries(new FormData(e.target))}); toast("Settings saved and pushed to edges"); } catch (err) { toast(err.message, true); } };
  $("#pForm").onsubmit = async e => { e.preventDefault(); try { await api("/auth/password", {method: "POST", body: Object.fromEntries(new FormData(e.target))}); toast("Password updated"); e.target.reset(); } catch (err) { toast(err.message, true); } };
};


pages.bandplan = resourcePage({
  path: "idens", title: "Band Plan Entry",
  columns: [{key: "id", label: "Iden"}, {key: "name", label: "Name"}, {label: "Base MHz", render: r => `<span class="mono">${Number(r.base_mhz).toFixed(5)}</span>`},
    {label: "Spacing", render: r => `${r.spacing_khz} kHz`}, {label: "Input offset", render: r => `${r.offset_mhz >= 0 ? "+" : ""}${r.offset_mhz} MHz`}, {label: "Bandwidth", render: r => `${r.bandwidth_khz} kHz`}],
  fields: [{name: "id", label: "Identity (0-15)", type: "number", required: true, readonly: true}, {name: "name", label: "Name", placeholder: "UHF 450"},
    {name: "base_mhz", label: "Base frequency MHz", type: "number", required: true}, {name: "spacing_khz", label: "Channel spacing kHz", type: "number", default: 6.25},
    {name: "offset_mhz", label: "Input offset MHz (RX = TX + offset)", type: "number", default: 5}, {name: "bandwidth_khz", label: "Bandwidth kHz", type: "number", default: 12.5}],
  newDefaults: {spacing_khz: 6.25, offset_mhz: 5, bandwidth_khz: 12.5},
});

pages.patches = resourcePage({
  path: "patches", title: "Patch",
  columns: [{key: "name", label: "Name"}, {label: "Side A", render: r => `${pill(r.mode_a, "mode")} TG ${esc(r.tg_a)}`}, {label: "", render: () => "⇄"},
    {label: "Side B", render: r => `${pill(r.mode_b, "mode")} TG ${esc(r.tg_b)}`}, {label: "Enabled", render: r => yes(r.enabled)}],
  fields: [{name: "name", label: "Name", required: true}, {name: "tg_a", label: "Talkgroup A", type: "select", options: "talkgroups", numeric: true},
    {name: "mode_a", label: "Mode A", type: "select", options: [{value: "DMR", label: "DMR"}, {value: "P25P1", label: "P25"}, {value: "ANALOG", label: "Analog"}]},
    {name: "tg_b", label: "Talkgroup B", type: "select", options: "talkgroups", numeric: true},
    {name: "mode_b", label: "Mode B", type: "select", options: [{value: "P25P1", label: "P25"}, {value: "DMR", label: "DMR"}, {value: "ANALOG", label: "Analog"}]},
    {name: "enabled", label: "Enabled", type: "bool", default: true}],
});

const LICENSE_NOTE = "DVMProject software is provided for personal, non-commercial, hobbyist use and must never be used for public safety or life safety critical applications. Acknowledging sets DVM's iAgreeNotToBeStupid flag in every generated config.";
pages.dvm = async el => {
  const [d, sites] = await Promise.all([api("/dvm/status"), api("/sites")]);
  const peers = d.fne?.peers || [];
  const warns = Object.entries(d.warnings || {}).flatMap(([k, v]) => v.map(m => `${k}: ${m}`));
  el.innerHTML = `
  ${d.license_acknowledged ? "" : `<div class="card" style="border-color:var(--warn);margin-bottom:16px"><h3 style="color:var(--warn)">DVM license acknowledgement required</h3>
    <p>${esc(LICENSE_NOTE)}</p><p class="muted">Until you acknowledge, KeyUp does not start the FNE, bridges or edge dvmhost instances.</p>
    <button class="btn primary" id="ackBtn">I acknowledge — personal, non-commercial use only</button></div>`}
  <div class="grid kpis">
    ${kpi(d.fne?.reachable ? pill("online") : pill("offline"), "FNE")}
    ${kpi(peers.length, "Peers connected")}
    ${kpi(d.fne?.affiliation_count ?? 0, "Affiliations")}
    ${kpi((d.fne?.active_calls || []).length, "Active calls")}
  </div>
  <div class="card" style="margin-top:16px"><h3>FNE peers</h3><div class="tablewrap"><table><thead><tr><th>Peer</th><th>Channel / bridge</th><th>Identity</th><th>Software</th><th>Address</th><th>Pings</th><th>Trunking</th></tr></thead><tbody>
    ${peers.map(p => `<tr><td class="mono">${esc(p.peer_id)}</td><td>${esc(p.label || "unknown peer")}</td><td>${esc(p.identity || "")}</td><td class="muted">${esc(p.software || "")}</td>
      <td class="mono">${esc(p.address || "")}</td><td>${esc(p.pings ?? "")}</td><td>${p.control_channel ? `voice ch of ${esc(p.control_channel)}` : ""}</td></tr>`).join("") || `<tr><td colspan="7" class="empty">${d.fne?.reachable ? "No peers connected yet" : "FNE not reachable: " + esc(d.fne?.error || "")}</td></tr>`}
  </tbody></table></div></div>
  <div class="grid two" style="margin-top:16px">
    <div class="card"><h3>Configuration checks</h3>${warns.length ? `<div class="warnlist">${warns.map(esc).join("<br>")}</div>` : pill("ok", "yes") + ' <span class="muted">no problems found</span>'}
      <p class="muted">Central DVM instances: ${d.central_instances.map(esc).join(", ") || "none"}<br>DVM source pinned to <span class="mono">${esc(d.dvm_ref.slice(0, 10))}</span></p></div>
    <div class="card"><h3>Generated configuration</h3>
      <div class="toolbar"><select id="pvSite"><option value="">Central (FNE + bridges)</option>${sites.map(s => `<option value="${s.id}">Site ${s.id} · ${esc(s.name)}</option>`).join("")}</select><button class="btn" id="pvGo">Show</button></div>
      <div id="pvOut" class="muted">Read-only preview of the files KeyUp writes for DVM.</div></div>
  </div>`;
  $("#ackBtn", el)?.addEventListener("click", async () => {
    if (!confirm(LICENSE_NOTE + "\n\nAcknowledge?")) return;
    await api("/settings", {method: "PUT", body: {dvm_license_acknowledged: "true"}}); toast("Acknowledged — starting DVM"); route();
  });
  $("#pvGo", el).onclick = async () => {
    const sid = $("#pvSite").value, files = await api("/dvm/preview" + (sid ? `?site_id=${sid}` : ""));
    $("#pvOut").innerHTML = Object.entries(files).map(([name, i]) => `<details><summary class="mono">${esc(name)} — ${esc(i.binary)} ${esc(i.args.join(" "))}</summary>
      ${Object.entries(i.files).map(([f, c]) => `<p class="mono">${esc(f)}</p><div class="code" style="max-height:300px;overflow:auto">${esc(c)}</div>`).join("")}</details>`).join("") || "Nothing generated for this selection.";
  };
};


async function pocActivation(unit) {
  if (!confirm(`Issue a PoC activation code for radio ${unit}? This enables PoC for the radio ID; the code works once and expires in 24 hours.`)) return;
  try {
    const a = await api(`/subscribers/${unit}/poc/activation`, {method: "POST"});
    const m = $("#modal"), f = $("#modalForm");
    f.innerHTML = `<h2>Activate a phone as radio ${esc(a.unit)} · ${esc(a.alias)}</h2>
      <div class="qrwrap"><div class="qr">${a.qr_svg}</div>
      <dl class="kv"><dt>Server</dt><dd>${esc(a.server)}</dd><dt>Radio ID</dt><dd>${esc(a.unit)}</dd>
      <dt>Code</dt><dd style="font-size:20px;letter-spacing:3px">${esc(a.code)}</dd><dt>Expires</dt><dd>${new Date(a.expires_at).toLocaleString()}</dd></dl></div>
      <p class="muted">In the KeyUp app tap <b>Scan activation QR code</b>, or enter the server, radio ID and code. The phone then follows the same rules as this radio ID: inhibit, permitted talkgroups and the radio ID lists on the FNE.</p>
      <div class="formfoot"><button class="btn primary" value="cancel">Done</button></div>`;
    m.showModal(); f.onsubmit = () => { m.close(); route(); };
  } catch (err) { toast(err.message, true); }
}

pages.poc = async el => {
  const [devs, st] = await Promise.all([api("/poc/devices"), api("/poc/status")]);
  const subs = Object.fromEntries((await api("/subscribers")).map(s => [s.id, s]));
  const em = Object.entries(st.emergencies || {});
  el.innerHTML = `${em.length ? `<div class="card" style="border-color:var(--emerg);margin-bottom:16px"><h3 style="color:var(--emerg)">Active emergencies</h3>
      ${em.map(([tg, e]) => `<div>TG ${esc(tg)} · unit ${esc(e.src)} (${esc(subs[e.src]?.alias || "")}) since ${new Date(e.ts * 1000).toLocaleTimeString()}</div>`).join("")}</div>` : ""}
  <div class="grid kpis">${kpi(st.sessions.length, "Phones online")}${kpi(devs.filter(d => !d.revoked).length, "Activated phones")}${kpi(st.sessions.filter(s => s.talking).length, "Transmitting")}</div>
  <div class="card" style="margin-top:16px"><h3>Online now</h3><div class="tablewrap"><table><thead><tr><th>Radio ID</th><th>Alias</th><th>Platform</th><th>Selected TG</th><th>Scan</th><th>Status</th><th>Connected</th></tr></thead><tbody>
  ${st.sessions.map(s => `<tr><td class="mono">${esc(s.unit)}</td><td>${esc(s.alias)}</td><td>${esc(s.platform)}</td><td>${esc(s.selected ?? "—")}</td><td>${esc((s.scan || []).join(", ") || "—")}</td>
    <td>${s.talking ? pill("TX TG " + s.talking, "denied") : pill("idle", "mode")}</td><td>${dur(s.connected_s)}</td></tr>`).join("") || `<tr><td colspan="7" class="empty">No phones connected</td></tr>`}
  </tbody></table></div></div>
  <div class="card" style="margin-top:16px"><h3>Activated phones</h3><p class="muted">Issue new activations from <a href="#/subscribers">Subscriber Radios</a> → PoC QR. Revoking logs the phone out immediately.</p>
  <div class="tablewrap"><table><thead><tr><th>Radio ID</th><th>Alias</th><th>Device</th><th>Platform / app</th><th>Activated</th><th>Last seen</th><th>State</th><th></th></tr></thead><tbody>
  ${devs.map(d => `<tr><td class="mono">${esc(d.subscriber_id)}</td><td>${esc(subs[d.subscriber_id]?.alias || "")}</td><td>${esc(d.name)}</td><td>${esc(d.platform)} ${esc(d.app_version)}</td>
    <td>${ago(d.created_at)}</td><td>${ago(d.last_seen)}</td><td>${d.revoked ? pill("revoked") : d.online ? pill("online") : pill("offline")}</td>
    <td class="actions">${d.revoked ? "" : `<button class="btn sm danger" data-revoke="${d.id}">Revoke</button>`}</td></tr>`).join("") || `<tr><td colspan="8" class="empty">No phones activated yet</td></tr>`}
  </tbody></table></div></div>`;
  el.onclick = async e => {
    const id = e.target.dataset.revoke;
    if (id && confirm("Revoke this phone? It is disconnected immediately and needs a new activation.")) {
      await api(`/poc/devices/${id}/revoke`, {method: "POST"}); toast("Revoked"); route();
    }
  };
};

function showAlarm(e) {
  const a = $("#alarm");
  if (!e.on) { a.classList.add("hidden"); return; }
  a.innerHTML = `⚠ EMERGENCY — TG ${esc(e.tg)} · unit ${esc(e.unit)} ${esc(e.alias || "")} (${esc(e.via || "radio")}) <button class="btn sm">Acknowledge</button>`;
  a.classList.remove("hidden");
  a.querySelector("button").onclick = () => a.classList.add("hidden");
}

// ------------------------------------------------------------------ router
const TITLES = {poc: "PoC Phones (Cellular Subscribers)", dashboard: "Dashboard", dvm: "DVM Network (FNE)", bandplan: "Band Plan (Iden Table)", patches: "Cross-mode Patches", sites: "Sites", devices: "Edge Devices & Enrollment", channels: "Channels & Frequency Plan", talkgroups: "Talkgroups",
  subscribers: "Subscriber Radios", sip: "SIP Interconnect", calls: "Call History", settings: "System Settings"};
async function route() {
  const page = (location.hash.slice(2).split("?")[0]) || "dashboard";
  document.querySelectorAll("aside nav a").forEach(a => a.classList.toggle("active", a.dataset.page === page));
  $("#pageTitle").textContent = TITLES[page] || page; $("#nav").classList.remove("open");
  const el = $("#page");
  try { await (pages[page] || pages.dashboard)(el); } catch (e) { el.innerHTML = `<div class="card err">${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", route);
async function start() {
  $("#login").classList.add("hidden"); $("#shell").classList.remove("hidden");
  connectLive(); route();
}
if (TOKEN) api("/auth/me").then(start).catch(logout); else logout();
