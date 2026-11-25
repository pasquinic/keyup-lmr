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
function getPath(obj, path) { return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj); }
function setPath(obj, path, val) {
  const keys = path.split("."), last = keys.pop();
  let o = obj;
  for (const k of keys) o = (o[k] ??= {});
  o[last] = val;
}
function orderedItem(v, label) {
  return `<li data-v="${esc(v)}"><span>${esc(label)}</span><button class="btn sm ghost" data-act="up" title="Move up">↑</button><button class="btn sm ghost" data-act="down" title="Move down">↓</button><button class="btn sm ghost" data-act="rm" title="Remove">✕</button></li>`;
}
async function formModal(title, fields, values = {}, submitLabel = "Save", onSubmit = null) {
  const m = $("#modal"), f = $("#modalForm");
  let html = `<h2>${esc(title)}</h2><div class="formgrid">`;
  let group = null;
  for (const fd of fields) {
    if (fd.group !== group) { if (group) html += `</fieldset>`; group = fd.group; if (group) html += `<fieldset><legend>${esc(group)}</legend>`; }
    const v = getPath(values, fd.name) ?? fd.default ?? "";
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
    if (fd.type === "ordered") {
      const opts = await options(fd.options);
      const label = new Map(opts.map(o => [String(o.value), o.label]));
      const items = (v || []).map(String).filter(x => label.has(x));
      html += `<div class="ordered wide" data-name="${esc(fd.name)}"><span>${esc(fd.label)}</span><ol>` +
        items.map(x => orderedItem(x, label.get(x))).join("") +
        `</ol><select data-add><option value="">+ add…</option>${opts.map(o => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("")}</select></div>`;
      continue;
    }
    if (fd.type === "textarea") { html += `<label${cls}>${esc(fd.label)}<textarea name="${fd.name}" rows="3">${esc(v)}</textarea></label>`; continue; }
    html += `<label${cls}>${esc(fd.label)}<input name="${fd.name}" type="${fd.type === "number" ? "number" : "text"}" step="any" value="${esc(v)}" ${fd.required ? "required" : ""} ${fd.readonly && values[fd.name] !== undefined ? "readonly" : ""} placeholder="${esc(fd.placeholder ?? "")}"></label>`;
  }
  if (group) html += `</fieldset>`;
  html += `</div><p class="err" id="formErr"></p><div class="formfoot"><button class="btn ghost" value="cancel" formnovalidate>Cancel</button><button class="btn primary" value="ok">${esc(submitLabel)}</button></div>`;
  f.innerHTML = html; m.showModal();
  f.querySelectorAll(".ordered").forEach(box => {
    const ol = box.querySelector("ol"), add = box.querySelector("[data-add]");
    add.onchange = () => {
      if (add.value && !ol.querySelector(`[data-v="${CSS.escape(add.value)}"]`))
        ol.insertAdjacentHTML("beforeend", orderedItem(add.value, add.selectedOptions[0].textContent));
      add.value = "";
    };
    ol.onclick = e => {
      const li = e.target.closest("li"), act = e.target.dataset.act;
      if (!li || !act) return;
      e.preventDefault();
      if (act === "up" && li.previousElementSibling) li.parentNode.insertBefore(li, li.previousElementSibling);
      if (act === "down" && li.nextElementSibling) li.parentNode.insertBefore(li.nextElementSibling, li);
      if (act === "rm") li.remove();
    };
  });
  const collect = () => {
    const out = {};
    const put = (name, val) => setPath(out, name, val);
    for (const fd of fields) {
      if (fd.type === "ordered") { put(fd.name, [...f.querySelectorAll(`.ordered[data-name="${CSS.escape(fd.name)}"] li`)].map(li => Number(li.dataset.v))); continue; }
      const el = f.elements[fd.name];
      if (fd.type === "bool") put(fd.name, el.checked);
      else if (fd.type === "multi") put(fd.name, [...el.selectedOptions].map(o => Number(o.value)));
      else if (fd.type === "number") put(fd.name, el.value === "" ? (fd.nullable ? null : 0) : Number(el.value));
      else if (fd.type === "select") put(fd.name, el.value === "" ? null : (fd.numeric ? Number(el.value) : el.value));
      else put(fd.name, el.value);
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
  <div class="grid kpis">
    ${kpi(`${online}/${d.sites.length}`, "Sites online")}
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
    <div class="card"><h3>Transcoding</h3><dl class="kv">
      <dt>DVSI vocoders</dt><dd>${d.transcoder.hardware_vocoders.length ? d.transcoder.hardware_vocoders.map(v => esc(v.port + " " + v.product)).join("<br>") : "none (IMBE/AMBE+2 conversions unavailable)"}</dd>
      <dt>Free / leased</dt><dd>${d.transcoder.free} / ${d.transcoder.leased}</dd>
      <dt>Active sessions</dt><dd>${d.transcoder.active_sessions}</dd>
      <dt>Transcoded calls</dt><dd>${d.transcoder.transcoded_calls}</dd>
      <dt>Passthrough</dt><dd>P25P2 ⇄ DMR ⇄ NXDN (AMBE+2)</dd></dl></div>
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
  <div class="meta">${esc(s.location || "")}<br>${s.channels} channel(s) · net codec ${esc(s.codec || "—")} · seen ${ago(s.last_seen)}</div></div>`;
};
function drawLive() {
  const box = $("#liveCalls"); if (!box) return;
  const calls = Object.values(liveCalls);
  if ($("#kActive")) $("#kActive").textContent = calls.length;
  box.innerHTML = calls.length ? calls.map(c => `<div class="callrow ${c.emergency ? "emerg" : ""} ${c.state === "hang" ? "hang" : ""}">
    <span class="tx" style="${c.state === "hang" ? "animation:none;background:var(--warn)" : ""}"></span>
    <div><div class="tg">TG ${esc(c.tg)} · ${esc(c.tg_name)} ${c.emergency ? pill("EMERGENCY", "emerg") : ""} ${c.state === "hang" ? pill("hang") : ""}</div>
    <div class="muted">${esc(c.src_alias)} @ ${esc(c.src_label)} → ${c.dests.map(esc).join(", ") || "no destinations"} · ${esc(c.codec)}${c.via_sip ? " · SIP" : ""}${c.linked && c.linked.length ? " · patched to TG " + c.linked.map(esc).join(", ") : ""}${c.parrot ? " · parrot" : ""}</div></div>
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
    else if (e.event === "poc") logEvent(`PoC ${e.alias || e.unit} ${e.action}${e.platform ? " (" + e.platform + ")" : ""}`, e.ts);
    else if (e.event === "console") logEvent(`Console ${e.name} ${e.action}${e.transport ? " via " + e.transport : ""}`, e.ts);
    else if (e.event === "inhibit") logEvent(`Radio ${e.unit} ${e.on ? "inhibited" : "uninhibited"} by ${e.by}`, e.ts);
    else if (e.event === "patch") logEvent(`Patch TG ${e.tgs.join(" + ")} ${e.on ? "linked" : "unlinked"} by ${e.by}`, e.ts);
    else if (e.event === "emergency") logEvent(`⚠ EMERGENCY ${e.on ? "declared" : "cleared"} TG ${e.tg} by ${e.alias || e.unit} (${e.via || "radio"})`, e.ts);
    else if (e.event === "recording_shipped" && !e.status.startsWith("sent")) logEvent(`Recording ${e.record_id} shipping: ${e.status}`, e.ts);
  };
}

pages.sites = resourcePage({
  path: "sites", title: "Site",
  columns: [{key: "id", label: "Site"}, {key: "name", label: "Name"}, {key: "location", label: "Location"}, {label: "Status", render: r => pill(r.status)},
    {key: "network_codec", label: "Net codec"}, {label: "Last seen", render: r => ago(r.last_seen)}],
  fields: [{name: "id", label: "Site number", type: "number", required: true, readonly: true}, {name: "name", label: "Name", required: true},
    {name: "location", label: "Location"}, {name: "rfss_id", label: "RFSS ID", type: "number", default: 1},
    {name: "network_codec", label: "Network codec", type: "select", options: ["AUTO", "ULAW", "PCM16", "IMBE", "AMBE2"], default: "AUTO"},
    {name: "callsign", label: "Station callsign (CW ID, blank = off)"}, {name: "latitude", label: "Latitude", type: "number"}, {name: "longitude", label: "Longitude", type: "number"},
    {name: "enabled", label: "Enabled", type: "bool", default: true}, {name: "notes", label: "Notes", type: "textarea", wide: true}],
});

pages.talkgroups = resourcePage({
  path: "talkgroups", title: "Talkgroup",
  columns: [{key: "id", label: "TGID"}, {key: "name", label: "Name"}, {key: "priority", label: "Priority"}, {key: "hang_time_ms", label: "Hang ms"},
    {label: "Emergency", render: r => yes(r.emergency_enabled)}, {label: "SIP", render: r => r.allow_sip ? `${pill("yes")} <span class="mono">${esc(r.sip_extension || r.id)}</span>` : yes(false)},
    {label: "Rules", render: r => [r.restricted && "restricted", r.affiliated_only && "affiliated only", r.parrot && "parrot", r.site_ids && "sites " + r.site_ids].filter(Boolean).map(esc).join(" · ") || '<span class="muted">open</span>'},
    {label: "Enabled", render: r => yes(r.enabled)}],
  fields: [{name: "id", label: "TGID", type: "number", required: true, readonly: true}, {name: "name", label: "Name", required: true},
    {name: "description", label: "Description", wide: true}, {name: "priority", label: "Priority (1-10)", type: "number", default: 5},
    {name: "hang_time_ms", label: "Hang time (ms)", type: "number", default: 3000}, {name: "emergency_enabled", label: "Emergency calls allowed", type: "bool", default: true},
    {name: "allow_sip", label: "Allow SIP interconnect", type: "bool"}, {name: "sip_extension", label: "SIP dial extension", placeholder: "e.g. 7101"},
    {name: "enabled", label: "Enabled", type: "bool", default: true},
    {name: "allow_poc", label: "PoC phones may use it", type: "bool", default: true},
    {name: "record", label: "Record audio (when recording is on)", type: "bool", default: true},
    {name: "restricted", label: "Restricted: only radios that list this TG may talk", type: "bool", group: "Routing rules"},
    {name: "affiliated_only", label: "Repeat only at sites where a radio on this TG was heard", type: "bool", group: "Routing rules"},
    {name: "parrot", label: "Parrot: echo each transmission back to the talker", type: "bool", group: "Routing rules"},
    {name: "site_ids", label: "Allowed sites (comma list, blank = all)", placeholder: "1,2", group: "Routing rules"}],
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
  {name: "signaling", label: "PTT / COR via", type: "select", options: [{value: "gpio", label: "GPIO pins"}, {value: "serial", label: "Serial port RTS (PTT) / CTS (COR)"}], default: "gpio", group: "Edge hardware"},
  {name: "serial_port", label: "Serial port (serial signalling)", placeholder: "/dev/ttyUSB0", group: "Edge hardware"},
  {name: "cor_invert", label: "Invert CTS (serial)", type: "bool", group: "Edge hardware"},
  {name: "interface", label: "Interface", type: "select", default: "radio", group: "Interface", options: [
    {value: "radio", label: "Radio: audio + PTT/COR (analog or vocoded on the edge)"},
    {value: "dfsi", label: "DFSI: P25 fixed station over IP (TIA-102.BAHA)"},
    {value: "ipsc", label: "IP Site Connect: MOTOTRBO repeaters (DMR)"},
    {value: "capplus", label: "Capacity Plus: MOTOTRBO Cap+ system (DMR)"}]},
  {name: "extra_talkgroups", label: "Packet: more talkgroups carried (comma list, * = all)", placeholder: "*", group: "Interface"},
  {name: "packet_config.local_port", label: "Packet: local UDP port the edge listens on", type: "number", group: "Interface"},
  {name: "packet_config.remote_host", label: "Packet: remote host (DFSI station / IPSC master)", group: "Interface"},
  {name: "packet_config.remote_port", label: "Packet: remote UDP port", type: "number", group: "Interface"},
  {name: "packet_config.role", label: "IPSC role", type: "select", default: "master", group: "Interface", options: [
    {value: "master", label: "KeyUp is the IPSC master (repeaters register to the edge)"}, {value: "peer", label: "KeyUp joins an existing master as a peer"}]},
  {name: "packet_config.peer_id", label: "IPSC / Cap+: KeyUp's peer (radio) ID", type: "number", group: "Interface"},
  {name: "packet_config.auth_key", label: "IPSC / Cap+: authentication key (hex, blank = none)", group: "Interface"},
  {name: "packet_config.timeslots", label: "IPSC / Cap+: timeslots carried", type: "select", default: "both", group: "Interface",
    options: [{value: "both", label: "Both"}, {value: "1", label: "Timeslot 1"}, {value: "2", label: "Timeslot 2"}]},
];
pages.channels = resourcePage({
  path: "channels", listPath: "frequencies", title: "Channel",
  columns: [{label: "Site", render: r => `${esc(r.site_id)} · ${esc(r.site_name)}`}, {key: "slot", label: "Port"}, {key: "name", label: "Name"}, {label: "Mode", render: r => pill(r.mode, "mode")},
    {label: "RX / TX MHz", render: r => `<span class="mono">${mhz(r.rx_mhz)} / ${mhz(r.tx_mhz)}</span>`}, {label: "Access", render: r => esc(r.mode.startsWith("P25") ? "NAC " + r.nac : r.mode === "DMR" ? `CC${r.color_code} TS${r.timeslot}` : r.mode === "NXDN" ? "RAN " + r.ran : r.ctcss_hz ? r.ctcss_hz + " Hz" : "CSQ")},
    {key: "talkgroup_id", label: "TG"}, {key: "codec", label: "Codec"},
    {label: "Band plan", render: r => r.identity !== null && r.identity !== undefined ? `<span class="mono">id ${r.identity} · ch ${r.channel_no}</span>` : '<span class="muted">—</span>'},
    {label: "Interface", render: r => r.interface && r.interface !== "radio"
      ? `${pill({dfsi: "DFSI", ipsc: "IPSC", capplus: "Cap+"}[r.interface] || r.interface, "mode")} <span class="mono">udp/${esc(r.packet_config?.local_port ?? "?")}${r.packet_config?.remote_host ? " → " + esc(r.packet_config.remote_host) + ":" + esc(r.packet_config.remote_port ?? "") : ""}</span>${r.extra_talkgroups ? ` <span class="muted">+TG ${esc(r.extra_talkgroups)}</span>` : ""}`
      : `<span class="mono">${r.signaling === "serial" ? `RTS/CTS ${esc(r.serial_port)}` : `GPIO ${r.ptt_gpio} / ${r.cor_gpio}`}</span>`},
    {label: "Checks", render: r => r.warnings.length ? `<div class="warnlist">${r.warnings.map(esc).join("<br>")}</div>` : pill("ok", "yes")}],
  fields: CHANNEL_FIELDS,
});

async function pocActivation(unit) {
  if (!confirm(`Issue a PoC activation code for radio ${unit}? This enables PoC for the radio ID; the code works once and expires in 24 hours.`)) return;
  try {
    const a = await api(`/subscribers/${unit}/poc/activation`, {method: "POST"});
    const m = $("#modal"), f = $("#modalForm");
    f.innerHTML = `<h2>Activate a phone as radio ${esc(a.unit)} · ${esc(a.alias)}</h2>
      <div class="qrwrap"><div class="qr">${a.qr_svg}</div>
      <dl class="kv"><dt>Server</dt><dd>${esc(a.server)}</dd><dt>Radio ID</dt><dd>${esc(a.unit)}</dd>
      <dt>Code</dt><dd style="font-size:20px;letter-spacing:3px">${esc(a.code)}</dd><dt>Expires</dt><dd>${new Date(a.expires_at).toLocaleString()}</dd></dl></div>
      <p class="muted">In the KeyUp app tap <b>Scan activation QR code</b>, or enter the server, radio ID and code. The phone then follows the same rules as this radio ID: inhibit, permitted talkgroups, zones and busy / priority rules.</p>
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

pages.consoles = async el => {
  await options("zones");
  await resourcePage({
    path: "consoles", title: "Dispatch Console",
    columns: [{key: "id", label: "#"}, {key: "name", label: "Name"},
      {label: "Radio ID", render: r => `<span class="mono">${esc(r.subscriber_id)}</span> ${esc(r.subscriber_alias)}`},
      {label: "Zones", render: r => r.zone_ids.length ? esc(r.zone_ids.map(id => cache.zones?.find(z => z.id === id)?.name ?? id).join(", ")) : '<span class="muted">radio ID\'s zones</span>'},
      {label: "Talkgroups", render: r => r.talkgroups.length ? esc(r.talkgroups.join(", ")) : '<span class="muted">none permitted</span>'},
      {label: "Rights", render: r => [r.can_inhibit && "inhibit", r.call_priority && "priority", r.can_patch && "patch"].filter(Boolean).join(" · ") || '<span class="muted">—</span>'},
      {label: "Tunnel", render: r => r.wg_ip ? `<span class="mono">${esc(r.wg_ip)}</span>${r.wg?.latest_handshake ? " · " + ago(new Date(r.wg.latest_handshake * 1000).toISOString()) : ""}` : '<span class="muted">—</span>'},
      {label: "Status", render: r => !r.enabled ? pill("disabled") : r.session ? `${pill("online")} <span class="muted">${esc(r.session.transport)}</span>` : r.activated ? pill("offline") : pill("not activated", "mode")},
      {label: "Last seen", render: r => r.last_seen ? ago(r.last_seen) : "—"}],
    rowActions: r => `<button class="btn sm" data-cact="${esc(r.id)}">Activate</button> ${r.activated ? `<button class="btn sm danger" data-crev="${esc(r.id)}">Revoke</button> ` : ""}`,
    fields: [{name: "name", label: "Name", required: true, placeholder: "Dispatch 1"},
      {name: "subscriber_id", label: "Transmits as radio ID", type: "select", options: "subscribers", numeric: true},
      {name: "zone_ids", label: "Zones on the console (none = the radio ID's zones)", type: "multi", options: "zones", wide: true},
      {name: "can_inhibit", label: "May inhibit / uninhibit radios", type: "bool", default: true},
      {name: "call_priority", label: "Call priority over radios (not over emergencies)", type: "bool", default: true},
      {name: "can_patch", label: "May patch talkgroups", type: "bool", default: true},
      {name: "enabled", label: "Enabled", type: "bool", default: true},
      {name: "notes", label: "Notes", type: "textarea", wide: true}],
  })(el);
  el.insertAdjacentHTML("afterbegin", `<div class="card" style="margin-bottom:16px"><h3>KeyUp Dispatch Console for Windows</h3>
    <p class="muted">A console transmits as its radio ID under the same talkgroup rules as a radio, monitors its zones' talkgroups at once, and (if allowed) inhibits radios, patches talkgroups and talks over radios.
    Install the console on the PC, press <b>Activate</b> here and enter the server and code in the console. The console sets up its own WireGuard tunnel
    (it asks for administrator rights once); without the tunnel it connects over HTTPS.</p></div>`);
  el.addEventListener("click", async e => {
    const act = e.target.dataset?.cact, rev = e.target.dataset?.crev;
    if (act) { e.stopPropagation(); await consoleActivation(act); }
    if (rev) {
      e.stopPropagation();
      if (!confirm("Revoke this console? Its login and tunnel stop working at once; activate it again to reconnect.")) return;
      try { await api(`/consoles/${rev}/revoke`, {method: "POST"}); toast("Revoked"); route(); } catch (err) { toast(err.message, true); }
    }
  }, true);
};

async function consoleActivation(id) {
  try {
    const a = await api(`/consoles/${id}/activation`, {method: "POST"});
    const m = $("#modal"), f = $("#modalForm");
    f.innerHTML = `<h2>Activate console ${esc(a.name)}</h2>
      <dl class="kv"><dt>Server</dt><dd class="mono">${esc(a.server)}</dd>
      <dt>Code</dt><dd style="font-size:22px;letter-spacing:3px" class="mono">${esc(a.code)}</dd><dt>Expires</dt><dd>${new Date(a.expires_at).toLocaleString()}</dd></dl>
      <p class="muted">In the KeyUp Dispatch Console choose <b>Connect to KeyUp…</b> and enter the server and code. The code works once; activating again moves the console to a new PC.</p>
      <div class="formfoot"><button class="btn primary" value="cancel">Done</button></div>`;
    m.showModal(); f.onsubmit = () => { m.close(); route(); };
  } catch (err) { toast(err.message, true); }
}

pages.zones = async el => {
  await options("zones");
  await options("talkgroups");
  await resourcePage({
    path: "zones", title: "Zone",
    extraToolbar: `<a class="btn" href="/api/zones/export.csv" id="zoneCsv" download>Export channel plan (CSV)</a>`,
    columns: [{key: "position", label: "Order"}, {label: "Zone", render: r => `${r.color ? `<span class="swatch" style="background:${esc(r.color)}"></span>` : ""}${esc(r.name)}`},
      {label: "Talkgroups (in order)", render: r => r.talkgroup_ids.length ? r.talkgroup_ids.map(t => `<span class="chip">${esc(t)} ${esc(cache.talkgroups.find(x => x.id === t)?.name ?? "")}</span>`).join(" ") : '<span class="muted">empty</span>'},
      {key: "notes", label: "Notes"}],
    fields: [{name: "name", label: "Name (as shown on radios, phones and consoles)", required: true},
      {name: "position", label: "Order (lower first)", type: "number", default: 0},
      {name: "color", label: "Tab colour (#RRGGBB, optional)", placeholder: "#1565C0"},
      {name: "talkgroup_ids", label: "Talkgroups, in channel order", type: "ordered", options: "talkgroups"},
      {name: "notes", label: "Notes", type: "textarea", wide: true}],
  })(el);
  el.insertAdjacentHTML("afterbegin", `<div class="card" style="margin-bottom:16px"><p class="muted">Zones group talkgroups like a radio codeplug. PoC phones and dispatch consoles show the zones of their radio ID
    (Subscriber Radios → Zones; none = all zones), each holding only the talkgroups that radio ID may use. The CSV export is a channel plan for programming radios.</p></div>`);
  $("#zoneCsv", el).onclick = async e => {
    e.preventDefault();
    const r = await fetch("/api/zones/export.csv", {headers: {Authorization: "Bearer " + TOKEN}});
    const url = URL.createObjectURL(await r.blob());
    Object.assign(document.createElement("a"), {href: url, download: "keyup-zones.csv"}).click();
    URL.revokeObjectURL(url);
  };
};

pages.subscribers = async el => {
  await options("zones");
  await resourcePage({
    path: "subscribers", title: "Subscriber",
    extraToolbar: `<button class="btn" id="importBtn">Import CSV</button>`,
    columns: [{key: "id", label: "Radio ID"}, {key: "alias", label: "Alias"}, {label: "Mode", render: r => pill(r.mode, "mode")}, {key: "model", label: "Model"},
      {key: "home_site_id", label: "Assigned site"}, {label: "Talkgroups", render: r => r.talkgroup_ids.length ? esc(r.talkgroup_ids.join(", ")) : '<span class="muted">all</span>'},
      {label: "Zones", render: r => r.zone_ids.length ? esc(r.zone_ids.map(id => cache.zones?.find(z => z.id === id)?.name ?? id).join(", ")) : '<span class="muted">all</span>'},
      {label: "PoC", render: r => yes(r.poc_enabled)},
      {label: "Last seen", render: r => r.last_seen ? `${ago(r.last_seen)} · ${r.last_site_id ? "site " + esc(r.last_site_id) : "PoC/console"} TG ${esc(r.last_tg_id ?? "")}` : "—"}, {label: "Enabled", render: r => yes(r.enabled)}],
    rowActions: r => `<button class="btn sm" data-poc="${esc(r.id)}">PoC QR</button> `,
    fields: [{name: "id", label: "Radio / unit ID", type: "number", required: true, readonly: true}, {name: "alias", label: "Alias", required: true},
      {name: "mode", label: "Mode", type: "select", options: MODES}, {name: "model", label: "Radio model"}, {name: "serial", label: "Serial"},
      {name: "home_site_id", label: "Assigned site / edge device", type: "select", options: "sites", nullable: true, numeric: true},
      {name: "talkgroup_ids", label: "Permitted talkgroups (none = all)", type: "multi", options: "talkgroups", wide: true},
      {name: "zone_ids", label: "Zones (none = all zones)", type: "multi", options: "zones", wide: true},
      {name: "enabled", label: "Enabled (inhibit when off)", type: "bool", default: true}, {name: "notify_site_trunking", label: "Send site-trunking data alerts", type: "bool", default: true},
      {name: "poc_enabled", label: "May use the PoC phone app", type: "bool"},
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


pages.bandplan = resourcePage({
  path: "bandplan", title: "Band Plan Entry",
  columns: [{key: "id", label: "Identity"}, {key: "name", label: "Name"}, {label: "Base MHz", render: r => `<span class="mono">${Number(r.base_mhz).toFixed(5)}</span>`},
    {label: "Spacing", render: r => `${r.spacing_khz} kHz`}, {label: "Input offset", render: r => `${r.offset_mhz >= 0 ? "+" : ""}${r.offset_mhz} MHz`}, {label: "Bandwidth", render: r => `${r.bandwidth_khz} kHz`}],
  fields: [{name: "id", label: "Identity (0-15)", type: "number", required: true, readonly: true}, {name: "name", label: "Name", placeholder: "UHF 450"},
    {name: "base_mhz", label: "Base frequency (MHz)", type: "number", required: true}, {name: "spacing_khz", label: "Channel spacing (kHz)", type: "number", default: 6.25},
    {name: "offset_mhz", label: "Input offset (MHz, RX = TX + offset)", type: "number", default: 5}, {name: "bandwidth_khz", label: "Bandwidth (kHz)", type: "number", default: 12.5}],
  newDefaults: {spacing_khz: 6.25, offset_mhz: 5, bandwidth_khz: 12.5},
});

pages.patches = resourcePage({
  path: "patches", title: "Patch",
  columns: [{key: "name", label: "Name"}, {label: "Talkgroup A", render: r => `TG ${esc(r.tg_a)}`}, {label: "", render: () => "⇄"},
    {label: "Talkgroup B", render: r => `TG ${esc(r.tg_b)}`}, {label: "Enabled", render: r => yes(r.enabled)}],
  fields: [{name: "name", label: "Name", required: true}, {name: "tg_a", label: "Talkgroup A", type: "select", options: "talkgroups", numeric: true},
    {name: "tg_b", label: "Talkgroup B", type: "select", options: "talkgroups", numeric: true}, {name: "enabled", label: "Enabled", type: "bool", default: true}],
});

pages.network = async el => {
  const [n, sites] = await Promise.all([api("/network"), api("/sites")]);
  el.innerHTML = `
  <div class="grid kpis">${kpi(n.sites.length, "Edges connected")}${kpi(n.sites.reduce((a, s) => a + s.channels.length, 0), "Channels up")}
    ${kpi(n.affiliations.length, "Affiliations")}${kpi(n.checks.length, "Config issues")}</div>
  ${n.sites.map(s => `<div class="card" style="margin-top:16px"><h3>${pill(s.mode === "site_trunking" ? "site_trunking" : "online")} Site ${esc(s.site_id)} · ${esc(s.label)}</h3>
    <dl class="kv"><dt>Edge</dt><dd>${esc(s.remote_ip)} · heartbeat ${s.heartbeat_age_s}s ago · v${esc(s.telemetry.version || "?")}${s.telemetry.cpu_temp_c ? ` · ${s.telemetry.cpu_temp_c}°C` : ""}</dd>
    <dt>Voice path</dt><dd>${s.voice_bound ? "bound" : "not bound"} · network codec ${esc(s.codec)} · vocoders ${esc(s.vocoders.join("/"))}</dd></dl>
    <table style="margin-top:8px"><thead><tr><th>Port</th><th>Channel</th><th>Mode</th><th>TG</th><th>Link</th><th>Receiving</th><th>Transmitting</th></tr></thead><tbody>
    ${s.channels.map(c => `<tr><td>${esc(c.slot ?? "")}</td><td>${esc(c.name)}</td><td>${pill(c.mode || "", "mode")}</td><td>${esc(c.tg ?? "—")}</td>
      <td>${c.interface ? `${pill(c.link_up ? "up" : "down", c.link_up ? "online" : "denied")} <span class="muted">${esc({dfsi: "DFSI " + (c.remote || ""), ipsc: "IPSC " + c.role + (c.peers?.length ? " · peers " + c.peers.join(", ") : ""), capplus: "Cap+ → " + (c.master || "")}[c.interface] || "")}</span>` : '<span class="muted">radio</span>'}</td>
      <td>${c.cor ? pill("receiving", "online") : '<span class="muted">idle</span>'}${c.last_call_in ? ` <span class="muted">last TG ${esc(c.last_call_in.tg)} · ${esc(c.last_call_in.src)}</span>` : ""}</td>
      <td>${c.ptt ? pill("keyed", "denied") : '<span class="muted">idle</span>'}</td></tr>`).join("") || `<tr><td colspan="7" class="empty">No channels reported</td></tr>`}
    </tbody></table></div>`).join("") || `<div class="card empty" style="margin-top:16px">No edge devices connected</div>`}
  <div class="grid two" style="margin-top:16px">
    <div class="card"><h3>Affiliations</h3><div class="tablewrap"><table><thead><tr><th>Radio</th><th>Site</th><th>TG</th><th>Heard</th></tr></thead><tbody>
      ${n.affiliations.map(a => `<tr><td>${esc(a.unit)} <span class="muted">${esc(a.alias)}</span></td><td>${esc(a.site)}</td><td>${esc(a.tg)}</td><td>${dur(a.age_s)} ago</td></tr>`).join("") || `<tr><td colspan="4" class="empty">No radios heard yet</td></tr>`}
    </tbody></table></div></div>
    <div class="card"><h3>Configuration checks</h3>${n.checks.length ? `<div class="warnlist">${n.checks.map(c => `<b>${esc(c.scope)}</b>: ${esc(c.message)}`).join("<br>")}</div>` : pill("ok", "yes") + ' <span class="muted">no problems found</span>'}</div>
  </div>
  <div class="card" style="margin-top:16px"><h3>Configuration pushed to an edge</h3>
    <div class="toolbar"><select id="pvSite">${sites.map(s => `<option value="${s.id}">Site ${s.id} · ${esc(s.name)}</option>`).join("")}</select><button class="btn" id="pvGo" ${sites.length ? "" : "disabled"}>Show</button></div>
    <div id="pvOut" class="muted">Read-only view of what the site's edge receives.</div></div>`;
  $("#pvGo", el).onclick = async () => {
    const cfg = await api(`/network/preview?site_id=${$("#pvSite").value}`);
    $("#pvOut").innerHTML = `<div class="code" style="max-height:420px;overflow:auto">${esc(JSON.stringify(cfg, null, 2))}</div>`;
  };
};

pages.calls = async el => {
  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  const filt = new URLSearchParams();
  for (const k of ["tg", "site", "unit", "result"]) if (q.get(k)) filt.set(k, q.get(k));
  for (const k of ["since", "until"]) if (q.get(k)) filt.set(k, new Date(q.get(k)).toISOString().slice(0, 19));  // local -> UTC
  if (q.get("recorded") === "1") filt.set("recorded", "true");
  const [rows, rs] = await Promise.all([api(`/calls?limit=300&${filt}`), api("/recording/status").catch(() => ({}))]);
  const ship = rs.shipping || {};
  const shipPill = c => !c.ship_status ? "" : c.ship_status === "sent" ? pill("shipped", "yes") : c.ship_status === "queued" ? pill("queued", "mode")
    : c.ship_status.startsWith("retry") ? pill(c.ship_status, "warn") : `<span title="${esc(c.ship_status)}">${pill("ship failed", "denied")}</span>`;
  const talkers = c => (c.talkers || []).map(t => `${esc(t.alias || t.src)} <span class="muted">(${esc(t.src)}) +${t.at}s</span>`).join("<br>");
  el.innerHTML = `<div class="card" style="margin-bottom:12px"><div class="toolbar" style="flex-wrap:wrap;gap:6px">
      <input id="fTg" placeholder="TG" value="${esc(q.get("tg") || "")}" style="width:80px">
      <input id="fSite" placeholder="Site" value="${esc(q.get("site") || "")}" style="width:70px">
      <input id="fUnit" placeholder="Radio ID" value="${esc(q.get("unit") || "")}" style="width:100px">
      <label class="muted">From <input id="fSince" type="datetime-local" value="${esc(q.get("since") || "")}"></label>
      <label class="muted">To <input id="fUntil" type="datetime-local" value="${esc(q.get("until") || "")}"></label>
      <select id="fRes"><option value="">All results</option><option value="granted" ${q.get("result") === "granted" ? "selected" : ""}>Calls</option><option value="denied" ${q.get("result") === "denied" ? "selected" : ""}>Denials</option></select>
      <label class="check"><input type="checkbox" id="fRec" ${q.get("recorded") === "1" ? "checked" : ""}> Recorded only</label>
      <button class="btn" id="fGo">Filter</button><button class="btn" id="fCsv">Export CSV</button></div>
    <p class="muted" style="margin:6px 0 0">Recording ${rs.enabled ? "on" : "off"} · ${esc(rs.files ?? 0)} files · ${((rs.bytes || 0) / 1048576).toFixed(1)} MB
      · shipping ${esc(ship.mode || "off")}${ship.target ? " → " + esc(ship.target) : ""}${ship.last_error ? ` · last error: ${esc(ship.last_error)}` : ""}</p>
    <audio id="player" controls style="width:100%;margin-top:8px;display:none"></audio></div>
  <div class="card tablewrap"><table><thead><tr><th>Start</th><th>TG</th><th>Talkers</th><th>Site</th><th>Duration</th><th>Destinations</th><th>Flags</th><th>Result</th><th>Recording</th></tr></thead><tbody>
  ${rows.map(c => `<tr><td>${new Date(c.started_at).toLocaleString()}</td><td>${esc(c.talkgroup_id)}</td>
    <td>${talkers(c) || esc(c.source_id || "—")}</td><td>${esc(c.source_site_id || (c.via_sip ? "SIP" : "—"))}</td>
    <td class="mono">${dur(c.duration_s)}</td><td class="muted">${esc(c.dest_sites.join(", "))}</td>
    <td>${c.emergency ? pill("EMERG", "emerg") : ""} ${c.transcoded ? pill("xcode", "mode") : ""} ${c.site_trunking ? pill("site trunking", "warn") : ""} ${c.via_sip ? pill("sip", "mode") : ""}</td>
    <td>${pill(c.result.split(":")[0], c.result.startsWith("denied") ? "denied" : c.result === "granted" ? "granted" : "warn")} <span class="muted">${esc(c.result.split(":")[1] || "")}</span></td>
    <td class="actions">${c.recording ? (c.recording.endsWith(".ul") ? `<button class="btn sm" data-play="${c.id}">▶ Play</button> ` : "") + `<button class="btn sm ghost" data-dl="${c.id}">Download</button> ${shipPill(c)}`
      + (c.ship_status && c.ship_status !== "sent" && c.ship_status !== "queued" ? ` <button class="btn sm ghost" data-ship="${c.id}">Retry</button>` : "")
      : `<span class="muted">${esc(c.recording_note || "—")}</span>`}</td></tr>`).join("") || `<tr><td colspan="9" class="empty">No calls</td></tr>`}
  </tbody></table></div>`;
  const params = () => {
    const p = new URLSearchParams();
    const set = (k, v) => v && p.set(k, v);
    set("tg", $("#fTg").value.trim()); set("site", $("#fSite").value.trim()); set("unit", $("#fUnit").value.trim());
    set("since", $("#fSince").value); set("until", $("#fUntil").value); set("result", $("#fRes").value);
    if ($("#fRec").checked) p.set("recorded", "1");
    return p;
  };
  $("#fGo", el).onclick = () => location.hash = `#/calls?${params()}`;
  const blob = async path => { const r = await fetch("/api" + path, {headers: {Authorization: "Bearer " + TOKEN}}); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText); return r; };
  $("#fCsv", el).onclick = async () => {
    const p = params(); if (p.get("recorded")) p.set("recorded", "true");
    for (const k of ["since", "until"]) if (p.get(k)) p.set(k, new Date(p.get(k)).toISOString().slice(0, 19));
    const r = await blob(`/calls/export.csv?${p}`);
    const url = URL.createObjectURL(await r.blob());
    Object.assign(document.createElement("a"), {href: url, download: "keyup-call-log.csv"}).click();
    URL.revokeObjectURL(url);
  };
  el.onclick = async e => {
    const {play, dl, ship: reship} = e.target.dataset;
    try {
      if (play) {
        const pl = $("#player"); const r = await blob(`/calls/${play}/audio`);
        if (pl.src) URL.revokeObjectURL(pl.src);
        pl.src = URL.createObjectURL(await r.blob()); pl.style.display = "block"; await pl.play();
      } else if (dl) {
        const r = await blob(`/calls/${dl}/audio`);
        const name = (r.headers.get("content-disposition") || "").match(/filename="([^"]+)"/)?.[1] || `call-${dl}.wav`;
        const url = URL.createObjectURL(await r.blob());
        Object.assign(document.createElement("a"), {href: url, download: name}).click(); URL.revokeObjectURL(url);
      } else if (reship) { await api(`/calls/${reship}/ship`, {method: "POST"}); toast("Queued for shipping"); route(); }
    } catch (err) { toast(err.message, true); }
  };
};

pages.settings = async el => {
  const s = await api("/settings");
  const labels = {system_name: "System name", wacn: "WACN (hex)", system_id: "System ID (hex)", require_known_subscriber: "Require known subscriber (true/false)",
    site_trunking_timeout_s: "Site trunking after link loss (s)", site_trunking_announce_interval_s: "Site trunking re-announce interval (s)",
    site_trunking_message: "Site trunking data message", wide_area_message: "Wide-area restored message", default_hang_time_ms: "Default hang time (ms)", max_call_s: "Max call duration (s)",
    affiliation_timeout_s: "Affiliation timeout (s)", cw_id_interval_s: "CW ID interval per transmitter (s)", parrot_delay_ms: "Parrot echo delay (ms)",
    recording_enabled: "Record talkgroup audio (true/false; per-talkgroup switch too)", recording_retention_days: "Keep recordings (days)",
    recording_max_gb: "Recording storage limit (GB, oldest deleted first)", recording_ship_mode: "Ship recordings: off | https | sip | siprec",
    recording_ship_target: "Ship to (https: URL · sip/siprec: sip:recorder@host:port)", recording_ship_transport: "SIP transport (udp | tcp)",
    recording_ship_username: "SIP auth username", recording_ship_password: "SIP auth password", recording_ship_token: "HTTPS bearer token"};
  const secret = k => /password|token/.test(k);
  el.innerHTML = `<div class="grid two"><div class="card"><h3>System</h3><form id="sForm" class="formgrid">${Object.entries(s).map(([k, v]) => `<label>${esc(labels[k] || k)}<input name="${esc(k)}" value="${esc(v)}" ${secret(k) ? 'type="password" autocomplete="new-password"' : ""}></label>`).join("")}
    <div class="wide"><button class="btn primary">Save &amp; push to sites</button></div></form></div>
    <div class="card"><h3>Change password</h3><form id="pForm" class="formgrid"><label class="wide">Current<input type="password" name="old"></label><label class="wide">New (8+ chars)<input type="password" name="new"></label><div><button class="btn">Update</button></div></form></div></div>`;
  $("#sForm").onsubmit = async e => { e.preventDefault(); try { await api("/settings", {method: "PUT", body: Object.fromEntries(new FormData(e.target))}); toast("Settings saved and pushed to edges"); } catch (err) { toast(err.message, true); } };
  $("#pForm").onsubmit = async e => { e.preventDefault(); try { await api("/auth/password", {method: "POST", body: Object.fromEntries(new FormData(e.target))}); toast("Password updated"); e.target.reset(); } catch (err) { toast(err.message, true); } };
};

// ------------------------------------------------------------------ router
const TITLES = {consoles: "Dispatch Consoles", zones: "Zones", poc: "PoC Phones (Cellular Subscribers)", network: "Network", bandplan: "Band Plan", patches: "Talkgroup Patches", dashboard: "Dashboard", sites: "Sites", devices: "Edge Devices & Enrollment", channels: "Channels & Frequency Plan", talkgroups: "Talkgroups",
  subscribers: "Subscriber Radios", sip: "SIP Interconnect", calls: "Call History & Recordings", settings: "System Settings"};
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
