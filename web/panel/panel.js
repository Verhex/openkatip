"use strict";
const sessionId = location.pathname.split("/").filter(Boolean).pop();
const proto = location.protocol === "https:" ? "wss" : "ws";
// İmzalı görüntüleme anahtarı (panel linkindeki ?t=): canlı akış, rapor ve görüntüler bununla açılır.
const viewToken = new URLSearchParams(location.search).get("t") || "";
function withToken(url) {
  if (!viewToken || !url || url.includes("t=")) return url;
  return url + (url.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(viewToken);
}
const srcPane = document.getElementById("srcPane");
const dstPane = document.getElementById("dstPane");
const stateEl = document.getElementById("state");
const rosterEl = document.getElementById("roster");

// Segment ve çeviriler aynı id ile birden çok kez gelir (büyüyerek); id'ye göre upsert.
const nodes = { src: new Map(), dst: new Map() };
const speakers = new Map();

function stick(pane) {
  const near = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 160;
  if (near) requestAnimationFrame(() => (pane.scrollTop = pane.scrollHeight));
}

function clearEmpty(pane) {
  const e = pane.querySelector(".empty");
  if (e) e.remove();
}

function upsertNode(map, pane, id) {
  let el = map.get(id);
  if (!el) {
    el = document.createElement("div");
    el.className = "seg";
    el.innerHTML = '<span class="who"></span><span class="txt"></span>';
    pane.appendChild(el);
    map.set(id, el);
  }
  return el;
}

function whoHtml(name, stamp) {
  return `${escapeHtml(name || "…")}` + (stamp ? `<span class="t">${stamp}</span>` : "");
}

function renderSegment(seg) {
  clearEmpty(srcPane);
  const el = upsertNode(nodes.src, srcPane, seg.id);
  const name = (seg.speaker || "…") + (seg.speaker_email ? ` <${seg.speaker_email}>` : "");
  speakers.set(seg.id, name);
  const stamp = seg.is_final
    ? new Date().toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" })
    : "";
  el.querySelector(".who").innerHTML = whoHtml(name, stamp);
  el.querySelector(".txt").textContent = seg.text;
  el.classList.toggle("partial", !seg.is_final);
  // Çeviri tarafındaki isim de güncel kalsın.
  const dst = nodes.dst.get(seg.id);
  if (dst) dst.querySelector(".who").innerHTML = whoHtml(name);
  stick(srcPane);
}

function renderTranslation(tr) {
  clearEmpty(dstPane);
  const el = upsertNode(nodes.dst, dstPane, tr.segment_id);
  el.querySelector(".who").innerHTML = whoHtml(speakers.get(tr.segment_id));
  el.querySelector(".txt").textContent = tr.text;
  el.classList.toggle("partial", !tr.is_final);
  stick(dstPane);
}

const feed = document.getElementById("feed");

function addNote(text, url) {
  const el = document.createElement("div");
  el.className = "note";
  el.innerHTML = `<b>Katip</b>`;
  el.appendChild(document.createTextNode(text));
  if (url) {
    const base = url.split("?")[0];
    for (const [label, href] of [["PDF", withToken(base)], ["Word", withToken(base.replace(/\.pdf$/, ".docx"))]]) {
      const a = document.createElement("a");
      a.href = href; a.target = "_blank"; a.rel = "noopener"; a.textContent = label;
      a.className = "report-link";
      el.appendChild(a);
    }
  }
  feed.appendChild(el);
  feed.scrollTop = feed.scrollHeight;
}

async function sessionHeaders() {
  try { localStorage.removeItem("katip-admin-token"); } catch (e) {}
  const me = await fetch("/api/auth/me", { credentials: "same-origin" });
  if (!me.ok) {
    addNote("Bu işlem için yönetim panelinde oturum açın.");
    return null;
  }
  const csrf = document.cookie.split("; ").find(x => x.startsWith("katip_csrf="));
  if (!csrf) return null;
  return { "X-CSRF-Token": decodeURIComponent(csrf.slice("katip_csrf=".length)) };
}

async function decide(id, action, el) {
  const headers = await sessionHeaders();
  if (!headers) return;
  const res = await fetch(`/api/approvals/${id}/${action}`, {
    method: "POST", headers,
  });
  if (res.status === 401) { try { localStorage.removeItem("katip-admin-token"); } catch (e) {} }
  el.textContent = res.ok ? (action === "approve" ? "Onaylandı." : "Reddedildi.") : `Hata: ${res.status}`;
}

const modeChat = document.getElementById("modeChat");
const modeVoice = document.getElementById("modeVoice");
const offrec = document.getElementById("offrec");

function showMode(mode) {
  modeChat.classList.toggle("on", mode === "chat");
  modeVoice.classList.toggle("on", mode === "voice");
  modeChat.setAttribute("aria-pressed", String(mode === "chat"));
  modeVoice.setAttribute("aria-pressed", String(mode === "voice"));
}

async function setMode(mode) {
  const headers = await sessionHeaders();
  if (!headers) return;
  const res = await fetch(`/api/meetings/${sessionId}/mode`, {
    method: "POST",
    headers: { ...headers, "content-type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  if (res.status === 401) { try { localStorage.removeItem("katip-admin-token"); } catch (e) {} }
  if (res.ok) showMode(mode); else addNote(`Mod değiştirilemedi: ${res.status}`);
}
modeChat.addEventListener("click", () => setMode("chat"));
modeVoice.addEventListener("click", () => setMode("voice"));

function addScreenshot(url, text) {
  const el = document.createElement("a");
  el.className = "shot";
  url = withToken(url);
  el.href = url;
  el.target = "_blank";
  el.rel = "noopener";
  const img = document.createElement("img");
  img.src = url;
  img.alt = text;
  el.append(img, document.createTextNode(text));
  feed.appendChild(el);
  feed.scrollTop = feed.scrollHeight;
}

function addApproval(id, text) {
  const el = document.createElement("div");
  el.className = "approval";
  el.appendChild(document.createTextNode(`Onay bekliyor — ${text}`));
  const ok = document.createElement("button"); ok.className = "ok"; ok.textContent = "Onayla";
  const no = document.createElement("button"); no.className = "no"; no.textContent = "Reddet";
  ok.addEventListener("click", () => decide(id, "approve", el));
  no.addEventListener("click", () => decide(id, "reject", el));
  el.append(ok, no);
  feed.appendChild(el);
  feed.scrollTop = feed.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function setState(text, cls) {
  stateEl.textContent = text;
  stateEl.className = "badge" + (cls ? " " + cls : "");
}

let ws, backoff = 500;
function connect() {
  ws = new WebSocket(`${proto}://${location.host}/ws/panel/${sessionId}?t=${encodeURIComponent(viewToken)}`);
  ws.onopen = () => { backoff = 500; setState("bağlı"); };
  ws.onclose = () => { setState("bağlantı koptu", "failed"); setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 10000); };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.kind === "segment") renderSegment(msg.segment);
    else if (msg.kind === "translation") renderTranslation(msg.translation);
    else if (msg.kind === "status") setState(msg.state === "active" ? "Canlı" : msg.state, msg.state === "active" ? "active" : msg.state === "failed" ? "failed" : "");
    else if (msg.kind === "roster") rosterEl.textContent = (msg.roster.participants || []).join(" · ");
    else if (msg.kind === "note") addNote(msg.text || "", msg.url);
    else if (msg.kind === "mode") showMode(msg.text);
    else if (msg.kind === "paused") offrec.hidden = msg.text !== "on";
    else if (msg.kind === "screenshot") addScreenshot(msg.url, msg.text || "Ekran görüntüsü");
    else if (msg.kind === "approval") addApproval(msg.approval_id, msg.text || "");
    else if (msg.kind === "error") setState(msg.text || "hata", "failed");
  };
}
connect();
