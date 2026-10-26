"use strict";
// Katip yönetim paneli. Veriler yalnız textContent ile basılır (XSS'e karşı);
// değişiklik yapan her istek CSRF anahtarını X-CSRF-Token başlığında gönderir.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
let accessMeetingId = null;

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)katip_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function api(method, path, body, raw) {
  const headers = {};
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken();
  let payload;
  if (raw) {
    payload = raw;
    headers["Content-Type"] = raw.type || "application/octet-stream";
  } else if (body !== undefined) {
    payload = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, { method, headers, body: payload, credentials: "same-origin" });
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    showLogin();
    throw new Error("Oturum sona erdi");
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* boş gövde */ }
  if (!res.ok) {
    const detail = data && data.detail;
    const msg = typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : `Hata (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(c ?? ""));
  return node;
}

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 3500);
}

function setResult(node, text, ok) {
  node.textContent = text;
  node.className = "result " + (ok === true ? "ok" : ok === false ? "bad" : "");
}

const fmtDate = (iso) => iso ? new Date(iso).toLocaleString("tr-TR", { dateStyle: "short", timeStyle: "short" }) : "–";
const usd = (v) => "$" + (v || 0).toFixed(3);
const STATE = { pending: "hazırlanıyor", joining: "katılıyor", waiting_admission: "lobide", active: "Canlı",
  ended: "bitti", failed: "başarısız" };

// ------------------------------------------------------------------ giriş
function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#password").value = "";
  $("#password").focus();
}

async function showApp() {
  const me = await api("GET", "/api/auth/me");
  if (me.role !== "admin") { location.replace("/hesap"); return; }
  $("#userEmail").textContent = me.email || "";
  $("#userRole").textContent = "Yönetici";
  $("#login").hidden = true;
  $("#app").hidden = false;
  $$("#tabs button").forEach(b => {
    b.classList.toggle("on", b.dataset.tab === "overview");
    if (b.classList.contains("on")) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  $$("section[data-view]").forEach(s => {
    s.hidden = s.dataset.view !== "overview";
  });
  $("#meetingAccess").hidden = true;
  $("#meetingMail").hidden = true;
  await Promise.all([loadOverview(), loadHealth(false), loadCalendar()]);
  refreshLogo();
}

$("#loginForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const button = ev.submitter || $("#loginForm button");
  button.disabled = true;
  $("#loginError").textContent = "";
  try {
    await api("POST", "/api/auth/login", { email: $("#loginEmail").value.trim(), password: $("#password").value });
    await showApp();
  } catch (e) {
    $("#loginError").textContent = e.message;
  } finally {
    button.disabled = false;
    $("#password").value = "";
  }
});

$("#logout").addEventListener("click", async () => {
  try { await api("POST", "/api/auth/logout"); } finally { showLogin(); }
});

// ------------------------------------------------------------------ sekmeler
$("#tabs").addEventListener("click", (ev) => {
  const tab = ev.target.closest("button[data-tab]");
  if (!tab) return;
  $$("#tabs button").forEach((b) => {
    b.classList.toggle("on", b === tab);
    if (b === tab) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  $$("section[data-view]").forEach((s) => { s.hidden = s.dataset.view !== tab.dataset.tab; });
  const loaders = { overview: () => Promise.all([loadOverview(), loadCalendar()]), meetings: loadHistory, usage: loadUsage, settings: loadSettings,
    branding: refreshLogo, people: loadPeople, security: loadSecurity, users: loadAccounts };
  loaders[tab.dataset.tab]?.().catch((e) => toast(e.message));
});

// ------------------------------------------------------------------ genel bakış
async function loadCalendar() {
  const status = await api("GET", "/api/integrations/calendar");
  $("#calendarStatus").textContent = !status.configured ? "Microsoft bağlantısı henüz kurulmadı." :
    `${status.mailbox} · politika: ${status.join_policy} · günlük otomatik sınır: ${status.daily_autojoins} · kuyruk sınırı: ${status.max_pending}`;
  $("#calendarRefresh").disabled = status.join_policy === "kapalı" || !status.configured;
  const data = await api("GET", "/api/integrations/calendar/queue");
  $("#calendarQueueWarning").hidden = !data.queue_full;
  $("#calendarQueueWarning").textContent = data.warning;
  $("#calendarUpcoming").replaceChildren(...data.meetings.map((m) =>
    el("li", {}, el("div", {}, `${m.title || "Toplantı"} · ${m.platform}`),
      el("div", {}, `${m.organizer_name} <${m.organizer_email}> · ${m.organizer_kind} · ${m.attendee_count} davetli`),
      el("div", {}, `${fmtDate(m.starts_at)} – ${fmtDate(m.ends_at)}`),
      el("div", {}, `${m.platform_host} · ${m.masked_url}`),
      el("div", {}, m.status === "approved" ? "Onaylandı; başlangıç bekleniyor." : `Neden onay gerekiyor: ${m.reason}`),
      m.status === "pending" ? el("button", {class: "small primary", onclick: ev => calendarDecision(m.id, "approve", ev.target)}, "Onayla") : "",
      " ", el("button", {class: "small danger", onclick: ev => calendarDecision(m.id, "reject", ev.target)}, "Reddet"),
      !m.trusted ? el("button", {class: "small", onclick: ev => trustCalendarOrganizer(m.organizer_email, ev.target)}, "Bu organizatöre her zaman güven") : "")));
  if (!data.meetings.length) $("#calendarUpcoming").append(el("li", {}, data.disabled ? data.reason : "Onay bekleyen veya onaylanmış davet yok."));
}

async function trustCalendarOrganizer(email, button) {
  if (!confirm(`${email} güven listesine eklensin mi? Güvenilir politikasında bu adresin davetleri otomatik katılım izni alır; onay politikasında yine onay gerekir.`)) return;
  button.disabled = true;
  try {
    await api("POST", "/api/admin/calendar/organizers/trust", {email});
    await loadCalendar();
    setResult($("#calendarResult"), "Organizatör güven listesine eklendi.", true);
  } catch (e) { setResult($("#calendarResult"), e.message, false); }
  finally { button.disabled = false; }
}

async function calendarDecision(id, action, button) {
  button.disabled = true;
  try {
    await api("POST", `/api/integrations/calendar/queue/${encodeURIComponent(id)}/${action}`);
    await loadCalendar();
    setResult($("#calendarResult"), action === "approve" ? "Oluşum onaylandı; Katip başlangıç penceresinde katılacak." : "Oluşum reddedildi; Katip katılmayacak.", true);
  } catch (e) { setResult($("#calendarResult"), e.message, false); }
  finally { button.disabled = false; }
}

$("#calendarRefresh").addEventListener("click", async () => {
  const button = $("#calendarRefresh");
  button.disabled = true;
  try {
    await api("POST", "/api/integrations/calendar/refresh");
    await loadCalendar();
    setResult($("#calendarResult"), "Takvim durumu güncellendi.", true);
  } catch (e) { setResult($("#calendarResult"), e.message, false); }
  finally { button.disabled = false; }
});

async function loadOverview() {
  const jobs = await api("GET", "/api/workers/jobs");
  const jobStates = {queued: "Sırada", running: "Çalışıyor", stopping: "Kapanış bekleniyor",
    lost: "Kapanış doğrulanamadı", cancelled: "İptal edildi", completed: "Tamamlandı", failed: "Başarısız",
    closed_manually: "Yönetici doğrulamasıyla kapatıldı"};
  $("#noWorkerJobs").hidden = jobs.length > 0;
  $("#workerJobs tbody").replaceChildren(...jobs.slice(0, 20).map((job) => el("tr", {},
    el("td", {}, job.session_id), el("td", {}, jobStates[job.status] || job.status),
    el("td", {}, job.heartbeat_at ? new Date(job.heartbeat_at).toLocaleString("tr-TR") : "–"),
    el("td", {}, ["queued", "running", "stopping"].includes(job.status)
      ? el("button", {class: "small danger", onclick: () => stopSession(job.session_id)}, "Çıkış iste")
      : job.status === "lost"
        ? el("button", {class: "small danger", onclick: () => resolveWorker(job.session_id)}, "Elle kapat") : ""))));
  const o = await api("GET", "/api/admin/overview");
  loadApprovals().catch((e) => toast(e.message));
  $("#brandName").textContent = o.company ? `${o.company} · Katip` : "Katip";
  $("#version").textContent = `sürüm ${o.version} · bot adı: ${o.bot_name}`;
  $("#statBots").textContent = `${o.bots.running}/${o.bots.limit}`;
  $("#statToday").textContent = `${Math.round(o.today_minutes)}/${o.daily_limit}`;
  const tbody = $("#sessions tbody");
  tbody.replaceChildren();
  $("#noSessions").hidden = o.sessions.length > 0;
  for (const s of o.sessions) {
    const actions = el("td", { class: "actions-cell" },
      el("button", { class: "small", onclick: () => openPanel(s.session_id) }, "Canlı panel"),
      el("button", { class: "small", onclick: () => openIdentities(s.session_id) }, "E-postalar"),
      s.mode === "tam" ? el("button", { class: "small",
        onclick: () => setMode(s.session_id, s.reply_mode === "voice" ? "chat" : "voice") },
        s.reply_mode === "voice" ? "Yazılıya al" : "Sesliye al") : "",
      el("button", { class: "small danger", onclick: () => stopSession(s.session_id) }, "Çıkar"));
    actions.append(el("button", {class: "small",
      onclick: () => openPresentation(s.session_id).catch(e => toast(e.message))}, "Sunum"));
    tbody.append(el("tr", {onclick: ev => {
      if (!ev.target.closest("button, a")) openMeetingDetail(s.session_id);
    }},
      el("td", {}, el("button", {class: "small", onclick: () => openMeetingDetail(s.session_id)}, s.title || s.session_id)),
      el("td", {}, s.owner || "sistem"),
      el("td", {}, el("span", { class: "badge " + (s.state === "active" ? "active" : s.state === "failed" ? "failed" : "") },
        STATE[s.state] || s.state), s.paused ? " · kayıt dışı" : ""),
      el("td", {}, s.mode === "kayit" ? "Sessiz kayıt" : `Tam (${s.reply_mode === "voice" ? "sesli" : "yazılı"})`),
      el("td", {}, `${Math.round(s.minutes)} dk`),
      el("td", {}, s.platform || "–"),
      el("td", {}, usd(s.cost_usd)),
      actions));
  }
}

async function loadHealth(refresh) {
  const h = await api("GET", "/api/admin/health" + (refresh ? "?refresh=true" : ""));
  const list = $("#health");
  list.replaceChildren();
  for (const c of h.checks) {
    const cls = c.ok === true ? "ok" : c.ok === false ? "bad" : "warn";
    const label = {ok: "Sağlıklı", bad: "Hata", warn: "Uyarı"}[cls];
    list.append(el("li", {}, el("span", { class: "dot " + cls, "aria-hidden": "true" }),
      el("div", {}, el("div", {class: "health-status"}, c.label,
        el("span", {class: "health-label " + cls}, label)), el("small", {}, c.detail))));
  }
  $("#healthAge").textContent = h.checked_seconds_ago ? `${h.checked_seconds_ago} sn önce kontrol edildi` : "şimdi kontrol edildi";
}
$("#healthRefresh").addEventListener("click", () => loadHealth(true).catch((e) => toast(e.message)));

$("#joinForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const out = $("#joinResult");
  const button = ev.submitter || $("#joinForm button");
  button.disabled = true;
  setResult(out, "Gönderiliyor…");
  try {
    const body = { url: $("#joinUrl").value.trim(), mode: $("#joinMode").value };
    const title = $("#joinTitle").value.trim();
    if (title) body.title = title;
    body.participants = parseParticipants($("#joinParticipants").value);
    const documents = Array.from($("#joinDocuments").files);
    if (documents.length > 8 || documents.some(f => f.size > 8 * 1024 * 1024)) {
      throw new Error("En fazla 8 belge; dosya başına en fazla 8 MB yükleyin.");
    }
    if (documents.length) {
      setResult(out, "Belgeler hazırlanıyor…", true);
      const preparation = await api("POST", "/api/preparations", { title });
      body.preparation_id = preparation.session_id;
      try {
        for (const file of documents) {
          const presentation = $("#joinPresentation").checked && file.name.toLowerCase().endsWith(".pdf");
          await api("POST", `/api/preparations/${preparation.session_id}/documents?filename=${encodeURIComponent(file.name)}&presentation=${presentation}`, undefined, file);
        }
      } catch (e) {
        await api("DELETE", `/api/preparations/${preparation.session_id}`).catch(() => {});
        throw e;
      }
    }
    const r = await api("POST", "/api/meetings", body);
    out.replaceChildren("Katip yola çıktı (", r.session_id, "). ",
      el("a", { href: r.panel_url, target: "_blank", rel: "noopener" }, "Canlı paneli aç"),
      " · lobiden kabul etmeyi unutmayın.");
    out.className = "result ok";
    $("#joinUrl").value = "";
    $("#joinTitle").value = "";
    $("#joinParticipants").value = "";
    $("#joinDocuments").value = "";
    $("#joinPresentation").checked = false;
    setTimeout(() => loadOverview().catch(() => {}), 1500);
  } catch (e) {
    setResult(out, e.message, false);
  } finally {
    button.disabled = false;
  }
});

async function openPanel(id) {
  window.open(`/panel/${encodeURIComponent(id)}`, "_blank", "noopener");
}

async function setMode(id, mode) {
  try {
    await api("POST", `/api/meetings/${encodeURIComponent(id)}/mode`, { mode });
    toast(mode === "voice" ? "Sesli cevap moduna alındı" : "Yazılı cevap moduna alındı");
    loadOverview();
  } catch (e) { toast(e.message); }
}

let presentationRefresh = null;
async function openPresentation(id) {
  clearInterval(presentationRefresh);
  const path = `/api/meetings/${encodeURIComponent(id)}/presentation`;
  const card = $("#meetingPresentation");
  const status = el("span", {class: "badge", role: "status"});
  const result = el("p", {class: "result"});
  const page = el("input", {type: "number", min: "1", max: "60", value: "1", "aria-label": "Slayt numarası"});
  const buttons = [];
  let busy = false;
  function render(state) {
    const label = {running: state.mode === "auto" ? "Anlatıyor" : "Gösteriliyor", paused: "Duraklatıldı", finished: "Bitti", off: "Sunum kapalı"}[state.state] || "Gösteriliyor";
    status.textContent = state.page && state.state !== "finished" ? `${label} · Slayt ${state.page} / ${state.pages}` : label;
    if (state.page) page.value = state.page;
    page.max = state.pages || 60;
    if (state.error) setResult(result, state.error, false);
  }
  async function run(action) {
    if (busy) return;
    busy = true;
    buttons.forEach(b => { b.disabled = true; });
    result.textContent = "İşleniyor…";
    try {
      const body = {action};
      if (action === "goto") body.page = Number(page.value);
      const state = await api("POST", path, body);
      render(state);
      if (!state.error) setResult(result, action === "stop" ? "Sunum bitti." : "Slayt gösteriliyor.", true);
    } catch (e) { setResult(result, e.message, false); }
    finally { busy = false; buttons.forEach(b => { b.disabled = false; }); }
  }
  const row = el("div", {class: "presentation-controls"});
  for (const [action, label] of [["start", "Başlat"], ["auto", "Otomatik sunum"], ["pause", "Duraklat"], ["resume", "Devam"], ["prev", "◀"], ["next", "▶"], ["goto", "Slayta git"], ["stop", "Bitir"]]) {
    const button = el("button", {class: "small", "aria-label": action === "prev" ? "Önceki slayt" : action === "next" ? "Sonraki slayt" : label,
      onclick: () => run(action)}, label);
    buttons.push(button);
    row.append(button);
  }
  const file = el("input", {type: "file", accept: ".pdf", "aria-label": "Sunum PDF'si"});
  const marked = el("input", {type: "checkbox"});
  const upload = el("button", {class: "small", onclick: async () => {
    if (!file.files[0] || !marked.checked) { setResult(result, "PDF seçin ve 'Sunum olarak kullan' işaretleyin.", false); return; }
    upload.disabled = true;
    try {
      await api("POST", `/api/preparations/${encodeURIComponent(id)}/documents?filename=${encodeURIComponent(file.files[0].name)}&presentation=true`, undefined, file.files[0]);
      setResult(result, "Sunum eklendi. Başlat düğmesine basabilirsiniz.", true);
    } catch (e) { setResult(result, e.message, false); }
    finally { upload.disabled = false; }
  }}, "PDF yükle");
  card.replaceChildren(el("h2", {}, "Katip sunar"), el("p", {class: "muted"}, id), status,
    row, page, el("label", {}, "Sunum PDF'si", file), el("label", {class: "check"}, marked, "Sunum olarak kullan"), upload, result);
  card.hidden = false;
  render(await api("GET", path));
  presentationRefresh = setInterval(async () => {
    if (busy || document.hidden) return;
    try { render(await api("GET", path)); }
    catch (e) { clearInterval(presentationRefresh); setResult(result, e.message, false); }
  }, 3000);
  card.scrollIntoView({block: "nearest"});
}

async function stopSession(id) {
  if (!confirm("Katip bu toplantıdan çıkarılsın mı? Rapor hazırlanacak.")) return;
  try {
    const result = await api("DELETE", `/api/meetings/${encodeURIComponent(id)}`);
    toast(result.stop_confirmed ? "Katip toplantıdan çıkarıldı" : "Çıkış istendi; kapanış doğrulaması bekleniyor.");
    loadOverview();
  } catch (e) { toast(e.message); }
}

async function resolveWorker(id) {
  if (!confirm("Worker sunucusunda Katip'in bu toplantıdan çıktığını ve ilgili sürecin/konteynerin kapandığını doğruladınız mı? Bu işlem yalnızca kapasiteyi serbest bırakır; çalışan worker'ı durdurmaz.")) return;
  try {
    await api("POST", `/api/workers/jobs/${encodeURIComponent(id)}/resolve`, {confirmation: "worker_stop_verified"});
    toast("İş yönetici doğrulamasıyla kapatıldı.");
    loadOverview();
  } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------------ toplantılar
async function loadHistory() {
  const { meetings } = await api("GET", "/api/admin/meetings?limit=50");
  const tbody = $("#history tbody");
  tbody.replaceChildren();
  for (const m of meetings) {
    const links = m.report_pdf
      ? el("td", {}, el("a", { href: m.report_pdf, target: "_blank", rel: "noopener" }, "PDF"), " · ",
          el("a", { href: m.report_docx, target: "_blank", rel: "noopener" }, "Word"))
      : el("td", { class: "muted" }, "yok");
    links.append(" ", el("button", { class: "small", onclick: () => openPanel(m.id) }, "Panel"));
    links.append(" ", el("button", { class: "small", onclick: () => loadMeetingMail(m.id).catch(e => toast(e.message)) }, "Toplantı notu maili"));
    links.append(" ", el("button", { class: "small", onclick: () => openIdentities(m.id) }, "E-postalar"),
      " ", el("button", { class: "small", onclick: () => loadMeetingAccess(m.id).catch(e => toast(e.message)) }, "Erişim izinleri"));
    tbody.append(el("tr", {},
      el("td", {}, fmtDate(m.started_at)),
      el("td", {}, el("button", {class: "small", onclick: () => openMeetingDetail(m.id)}, m.title || m.id)),
      el("td", {}, m.minutes != null ? `${m.minutes} dk` : "–"),
      el("td", {}, STATE[m.state] || m.state || "–"),
      el("td", {}, m.owner_account_id == null ? "Sahipsiz" : `Hesap #${m.owner_account_id}`),
      links));
  }
}

async function loadMeetingAccess(id) {
  const [access, accounts] = await Promise.all([
    api("GET", `/api/meetings/${encodeURIComponent(id)}/grants`), api("GET", "/api/auth/accounts")
  ]);
  accessMeetingId = id;
  $("#meetingAccess").hidden = false;
  $("#meetingAccessOwner").textContent = `${id} · Sahip: ${access.owner_email || "Sahipsiz (yalnız yönetici ve izin verilen hesaplar)"}`;
  $("#meetingGrantAccount").replaceChildren(...accounts.filter(a => a.enabled).map(a =>
    el("option", {value: a.id}, `${a.email} (${ROLE_LABELS[a.role]})`)));
  $("#meetingGrants").replaceChildren(...access.grants.map(g => el("li", {},
    `${g.email || g.account_id} — ${g.permission === "manage" ? "Yönetme" : "Okuma"} `,
    el("button", {class: "small danger", onclick: async () => {
      try {
        await api("DELETE", `/api/meetings/${encodeURIComponent(id)}/grants/${g.account_id}`);
        await loadMeetingAccess(id);
      } catch (e) { setResult($("#meetingGrantResult"), e.message, false); }
    }}, "İzni kaldır"))));
  if (!access.grants.length) $("#meetingGrants").append(el("li", {}, "Ek izin yok."));
}

async function loadMeetingMail(id) {
  const path = `/api/meetings/${encodeURIComponent(id)}/mail`;
  const draft = await api("GET", path);
  renderMeetingMail(id, path, draft);
}

function renderMeetingMail(id, path, draft) {
  const card = $("#meetingMail");
  card.hidden = false;
  const sent = draft.status === "gönderildi";
  const locked = sent || draft.status === "gönderiliyor";
  const labels = {taslak: "Taslak", gönderiliyor: "Gönderiliyor", gönderildi: "Gönderildi", hata: "Hata"};
  card.replaceChildren(el("div", {class: "card-head"}, el("h2", {}, "Toplantı notu maili"),
    el("span", {class: "badge " + (sent ? "healthy" : draft.status === "hata" ? "failed" : "")},
      (labels[draft.status] || draft.status) + (sent ? " " + fmtDate(draft.sent_at) : ""))),
    el("p", {class: "muted"}, id));
  if (draft.error) card.append(el("p", {class: "error", role: "alert"}, draft.error));
  if (!draft.can_manage) {
    card.scrollIntoView({block: "nearest"});
    return;
  }
  let current = draft;
  const list = el("div", {class: "stack", role: "group", "aria-label": "Mail alıcıları"});
  const selected = new Set(draft.recipients);
  const candidates = new Map(draft.candidates.map(c => [c.email, c]));
  function addCandidate(c) {
    const input = el("input", {type: "checkbox", value: c.email});
    input.checked = selected.has(c.email);
    input.disabled = locked;
    list.append(el("label", {class: "check"}, input, `${c.name} <${c.email}> `,
      el("span", {class: "badge " + (c.internal ? "" : "offrec")}, c.internal ? "İç" : "Harici")));
  }
  draft.candidates.forEach(addCandidate);
  const extra = el("input", {type: "email", maxlength: "320", placeholder: "ad@şirket.com", "aria-label": "İç e-posta adresi"});
  const note = el("textarea", {maxlength: "1000", rows: "3"});
  note.value = draft.intro;
  const preview = el("pre", {class: "mail-preview"}, draft.body_text);
  const result = el("p", {class: "result", role: "status"});
  const send = el("button", {class: "primary", type: "button"}, "Onayla ve gönder");
  const save = el("button", {type: "button"}, "Taslağı kaydet");
  const add = el("button", {type: "button"}, "İç adres ekle");
  const controls = [note, extra, add, save, send];
  function busy(value) {
    controls.forEach(n => { n.disabled = locked || value; });
    list.querySelectorAll("input").forEach(n => { n.disabled = locked || value; });
    send.disabled = locked || value || Boolean(current.disabled_reason);
  }
  async function saveDraft(additional) {
    const recipients = Array.from(list.querySelectorAll("input:checked"), n => n.value);
    if (additional) recipients.push(additional);
    current = await api("PUT", path, {draft_id: current.draft_id, version: current.version, recipients, intro: note.value});
    preview.textContent = current.body_text;
    return current;
  }
  add.addEventListener("click", async () => {
    if (!extra.checkValidity() || !extra.value.trim()) { extra.reportValidity(); return; }
    busy(true);
    try {
      const updated = await saveDraft(extra.value.trim());
      renderMeetingMail(id, path, updated);
    } catch (e) { setResult(result, e.message, false); }
    finally { busy(false); }
  });
  save.addEventListener("click", async () => {
    busy(true);
    try { await saveDraft(); setResult(result, "Taslak kaydedildi.", true); }
    catch (e) { setResult(result, e.message, false); }
    finally { busy(false); }
  });
  send.addEventListener("click", async () => {
    busy(true);
    try {
      await saveDraft();
      const external = current.recipients.filter(email => !candidates.get(email)?.internal);
      const warning = external.length ? `\nHarici alıcılar:\n${external.join("\n")}` : "\nHarici alıcı yok.";
      if (!confirm(`${current.recipients.length} alıcıya toplantı notunu göndermeyi onaylıyor musunuz?${warning}`)) return;
      const updated = await api("POST", path + "/send", {draft_id: current.draft_id, version: current.version});
      renderMeetingMail(id, path, updated);
    } catch (e) {
      try { await loadMeetingMail(id); } catch (_) { /* mevcut taslak korunur */ }
      toast(e.message);
    } finally { busy(false); }
  });
  card.append(el("h3", {}, "Alıcılar (en fazla 50)"), list,
    el("div", {class: "actions"}, extra, add),
    el("label", {}, "Giriş notu (isteğe bağlı, en fazla 1000 karakter)", note),
    el("h3", {}, current.subject), preview,
    el("p", {class: "fine"}, "PDF raporu eklenir. Ek veya istek sınırı aşılırsa yalnız panel raporu bağlantısı gönderilir. Bağlantıyı açmak için panel erişimi gerekir."));
  if (current.disabled_reason) card.append(el("p", {class: "muted"}, current.disabled_reason));
  card.append(el("div", {class: "actions"}, save, send), result);
  busy(false);
  card.scrollIntoView({block: "nearest"});
}

$("#meetingGrantForm").addEventListener("submit", async ev => {
  ev.preventDefault();
  if (!accessMeetingId) return;
  try {
    await api("POST", `/api/meetings/${encodeURIComponent(accessMeetingId)}/grants`, {
      account_id: Number($("#meetingGrantAccount").value), permission: $("#meetingGrantPermission").value
    });
    await loadMeetingAccess(accessMeetingId);
    setResult($("#meetingGrantResult"), "Toplantı izni kaydedildi.", true);
  } catch (e) { setResult($("#meetingGrantResult"), e.message, false); }
});

// ------------------------------------------------------------------ kullanım
async function loadUsage() {
  const days = $("#usageDays").value;
  const u = await api("GET", `/api/usage?days=${days}`);
  const t = u.totals;
  $("#usageTotals").replaceChildren(
    ...[[t.meetings, "toplantı"], [Math.round(t.minutes), "dakika"], [t.llm_calls, "Claude çağrısı"],
      [usd(t.usd.total), "toplam tahmini"]].map(([v, l]) => el("div", {}, el("span", { class: "stat" }, String(v)), el("span", { class: "muted" }, l))));
  const tbody = $("#usage tbody");
  tbody.replaceChildren();
  for (const m of u.meetings) {
    tbody.append(el("tr", {},
      el("td", {}, fmtDate(m.started_at)), el("td", {}, m.title || m.meeting_id),
      el("td", {}, String(m.minutes)), el("td", {}, String(m.llm_calls)),
      el("td", {}, usd(m.usd.claude)), el("td", {}, usd(m.usd.stt)), el("td", {}, usd(m.usd.tts)),
      el("td", {}, usd(m.usd.total))));
  }
}
$("#usageDays").addEventListener("change", () => loadUsage().catch((e) => toast(e.message)));

// ------------------------------------------------------------------ ayarlar
const NUMBER_FIELDS = ["retention_days", "share_link_days", "max_meeting_minutes", "record_max_minutes",
  "daily_meeting_minutes", "max_agent_calls_per_meeting", "calendar_daily_autojoins", "calendar_max_pending"];
const LIST_FIELDS = ["calendar_organizer_domains", "calendar_organizer_emails"];

function calendarPolicyWarning() {
  $("#calendarPolicyWarning").hidden = $("#settingsForm").elements.calendar_join_policy.value !== "hepsi";
  syncCalendarPolicy();
}
// Keep the original select as the sole named form field and source of truth.
const policySelect = $("#settingsForm").elements.calendar_join_policy;
const policyField = el("div", {class: "policy-field"}, el("span", {id: "policyLabel"}, "Politika"));
policySelect.parentElement.replaceWith(policyField);
policyField.append(policySelect);
const policyControl = el("div", {class: "segmented", role: "radiogroup", "aria-labelledby": "policyLabel"});
const policyLabels = ["Kapalı", "Onay (önerilen)", "Güvenilir", "Hepsi"];
const policyButtons = Array.from(policySelect.options).map((option, index) =>
  el("button", {type: "button", role: "radio", "aria-label": option.textContent,
    onclick: () => chooseCalendarPolicy(index)}, policyLabels[index]));
policyControl.append(...policyButtons);
policyField.append(policyControl);
policySelect.hidden = true;
function syncCalendarPolicy() {
  policyButtons.forEach((button, index) => {
    const selected = policySelect.selectedIndex === index;
    button.setAttribute("aria-checked", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
}
function chooseCalendarPolicy(index) {
  policySelect.selectedIndex = index;
  policySelect.dispatchEvent(new Event("change", {bubbles: true}));
}
policyControl.addEventListener("keydown", ev => {
  const keys = ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"];
  if (!keys.includes(ev.key)) return;
  ev.preventDefault();
  const count = policyButtons.length;
  const index = ev.key === "Home" ? 0 : ev.key === "End" ? count - 1 :
    (policySelect.selectedIndex + (["ArrowLeft", "ArrowUp"].includes(ev.key) ? -1 : 1) + count) % count;
  chooseCalendarPolicy(index);
  policyButtons[index].focus();
});
syncCalendarPolicy();
$("#settingsForm").elements.calendar_join_policy.addEventListener("change", calendarPolicyWarning);

async function loadSettings() {
  const { values } = await api("GET", "/api/admin/settings");
  const form = $("#settingsForm");
  for (const [key, value] of Object.entries(values)) {
    const input = form.elements[key];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else if (LIST_FIELDS.includes(key)) input.value = value.join("\n");
    else input.value = value ?? "";
  }
  calendarPolicyWarning();
}

$("#settingsForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const out = $("#settingsResult");
  const body = {};
  for (const input of form.elements) {
    if (!input.name) continue;
    if (input.type === "checkbox") body[input.name] = input.checked;
    else if (NUMBER_FIELDS.includes(input.name)) body[input.name] = Number(input.value);
    else if (LIST_FIELDS.includes(input.name)) body[input.name] = input.value.split(/\r?\n/).map(v => v.trim()).filter(Boolean);
    else body[input.name] = input.value.trim();
  }
  try {
    await api("PUT", "/api/admin/settings", body);
    setResult(out, "Kaydedildi. Yeni toplantılar bu ayarlarla başlar.", true);
    loadOverview().catch(() => {});
    loadCalendar().catch((e) => toast(e.message));
  } catch (e) {
    setResult(out, e.message, false);
  }
});

// ------------------------------------------------------------------ marka
function refreshLogo() {
  const src = "/branding/logo?ts=" + Date.now();
  const probe = new Image();
  probe.onload = () => {
    for (const id of ["#logo", "#loginLogo", "#logoPreview"]) { $(id).src = src; $(id).hidden = false; }
    $("#mark").hidden = true; $("#loginMark").hidden = true; $("#logoEmpty").hidden = true;
  };
  probe.onerror = () => {
    for (const id of ["#logo", "#loginLogo", "#logoPreview"]) $(id).hidden = true;
    $("#mark").hidden = false; $("#loginMark").hidden = false; $("#logoEmpty").hidden = false;
  };
  probe.src = src;
  return Promise.resolve();
}

$("#logoUpload").addEventListener("click", async () => {
  const file = $("#logoFile").files[0];
  const out = $("#logoResult");
  if (!file) { setResult(out, "Önce bir dosya seçin.", false); return; }
  if (file.size > 1024 * 1024) { setResult(out, "Dosya 1 MB'tan büyük.", false); return; }
  try {
    await api("POST", "/api/admin/branding/logo", undefined, file);
    setResult(out, "Logo yüklendi.", true);
    refreshLogo();
  } catch (e) { setResult(out, e.message, false); }
});

$("#logoRemove").addEventListener("click", async () => {
  if (!confirm("Logo kaldırılsın mı?")) return;
  try {
    await api("DELETE", "/api/admin/branding/logo");
    setResult($("#logoResult"), "Logo kaldırıldı.", true);
    refreshLogo();
  } catch (e) { setResult($("#logoResult"), e.message, false); }
});

// ------------------------------------------------------------------ kişiler
async function loadPeople() {
  const people = await api("GET", "/api/people");
  const tbody = $("#people tbody");
  tbody.replaceChildren();
  $("#noPeople").hidden = people.length > 0;
  for (const p of people) {
    tbody.append(el("tr", {},
      el("td", {}, p.name), el("td", {}, p.email || "–"), el("td", {}, p.role),
      el("td", {}, p.voice_enrolled ? "kayıtlı (şifreli)" : "yok"),
      el("td", { class: "actions-cell" }, el("button", { class: "small danger", onclick: () => deletePerson(p) }, "Sil"))));
  }
}

async function deletePerson(p) {
  if (!confirm(`${p.name} silinsin mi? Ses izi ve profil silinir, geçmişte adı anonimleşir.`)) return;
  try {
    const result = await api("DELETE", `/api/people/${p.id}`);
    toast(result.forgotten ? "Silindi" : "Silme henüz tamamlanamadı; kalan kayıtlar otomatik yeniden deneniyor.");
    loadHealth(true).catch((e) => toast(e.message));
    loadPeople();
  } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------------ güvenlik
const EVENTS = { admin_login: "Giriş", admin_login_failed: "Hatalı parola", admin_login_locked: "Kilitli giriş denemesi",
  admin_password_changed: "Parola değişti", admin_logout_all: "Tüm oturumlar kapatıldı",
  settings_changed: "Ayarlar değişti", logo_changed: "Logo değişti" };

async function loadSecurity() {
  const [{ sessions }, { events }] = await Promise.all([api("GET", "/api/admin/admin-sessions"), api("GET", "/api/admin/logins")]);
  $("#adminSessions").replaceChildren(...sessions.map((s) =>
    el("li", {}, `Açılış ${fmtDate(s.created_at)} · son işlem ${fmtDate(s.last_seen)}`,
      el("small", {}, `${(s.user_agent || "").slice(0, 90)} · istemci ${s.client || "?"}`))));
  $("#events").replaceChildren(...events.map((e) =>
    el("li", {class: "event-row"},
      el("span", {class: "dot " + (/failed|locked/.test(e.action) ? "bad" : ""), "aria-hidden": "true"}),
      el("div", {}, EVENTS[e.action] || e.action, el("span", {class: "mono"}, ` · ${fmtDate(e.at)}`),
        el("small", {}, e.detail && e.detail.client ? `istemci ${e.detail.client}` : e.detail && e.detail.keys ? e.detail.keys.join(", ") : "")))));
}

$("#passwordForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const out = $("#passwordResult");
  if (f.elements.new.value !== f.elements.again.value) { setResult(out, "Yeni parolalar aynı değil.", false); return; }
  try {
    await api("POST", "/api/auth/password", { current: f.elements.current.value, new: f.elements.new.value });
    f.reset();
    toast("Parola değişti. Yeniden giriş yapın.");
    showLogin();
  } catch (e) { setResult(out, e.message, false); }
});

$("#logoutAll").addEventListener("click", async () => {
  if (!confirm("Bu dahil tüm panel oturumları kapatılsın mı?")) return;
  try { await api("POST", "/api/auth/logout-all"); } finally { showLogin(); }
});

function parseParticipants(text) {
  return text.split(/\r?\n/).filter((line) => line.trim()).map((line) => {
    const match = line.trim().match(/^(.+?)\s*<([^<>]+)>$/);
    if (!match) throw new Error("Her katılımcıyı Ad Soyad <mail@şirket.com> biçiminde yazın.");
    return { name: match[1].trim(), email: match[2].trim() };
  });
}

async function loadApprovals() {
  const pending = await api("GET", "/api/approvals");
  $("#approvals").replaceChildren(...pending.map((a) => el("li", {},
    `${a.session_id}: ${a.description} `,
    el("button", { class: "small", onclick: () => decideApproval(a.id, "approve") }, "Onayla"),
    " ", el("button", { class: "small danger", onclick: () => decideApproval(a.id, "reject") }, "Reddet"))));
  if (!pending.length) $("#approvals").append(el("li", {}, "Bekleyen talimat yok."));
}

async function decideApproval(id, decision) {
  try {
    await api("POST", `/api/approvals/${encodeURIComponent(id)}/${decision}`);
    await loadApprovals();
  } catch (e) { toast(e.message); }
}
$("#approvalsRefresh").addEventListener("click", () => loadApprovals().catch((e) => toast(e.message)));

function identityPath() {
  const id = $("#identityMeeting").value.trim();
  if (!id) throw new Error("Toplantı ID girin.");
  return `/api/meetings/${encodeURIComponent(id)}/participants`;
}

async function loadIdentities() {
  const data = await api("GET", identityPath());
  $("#identityName").replaceChildren(...data.participants.map((p) =>
    el("option", { value: p.name }, `${p.name} — ${p.email || "eşleşmedi"}`)));
  $("#identityCandidates").replaceChildren(...data.identities.map((i) =>
    el("li", {}, `${i.name} <${i.email}> (${i.source === "calendar" ? "takvim daveti" : "katılımcı listesi"}) `,
      el("button", { type: "button", class: "small", onclick: () => {
        $("#identityEmail").value = i.email;
        const option = Array.from($("#identityName").options).find((o) => o.value === i.name);
        if (option) $("#identityName").value = option.value;
      } }, "Seç"))));
  setResult($("#identityResult"), "Davetli listesi gerçek katılımı göstermez. Toplantıda gözlenen kişiyle e-postayı eşleştirin.");
}

async function openIdentities(id) {
  $("#tabs button[data-tab=overview]").click();
  $("#identityMeeting").value = id;
  try { await loadIdentities(); $("#identityLoadForm").scrollIntoView({ behavior: "smooth" }); }
  catch (e) { toast(e.message); }
}
$("#identityLoadForm").addEventListener("submit", (ev) => {
  ev.preventDefault(); loadIdentities().catch((e) => toast(e.message));
});
$("#microsoftSync").addEventListener("click", async (ev) => {
  ev.target.disabled = true;
  try {
    const result = await api("POST", identityPath() + "/microsoft-sync");
    await loadIdentities();
    setResult($("#identityResult"), !result.configured ? "Microsoft 365 bağlantısı henüz yapılandırılmadı."
      : result.error || `${result.imported} davetli e-postası getirildi.`, !result.error && result.configured);
  } catch (e) { setResult($("#identityResult"), e.message, false); }
  finally { ev.target.disabled = false; }
});
$("#identityBindForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const button = ev.submitter;
  button.disabled = true;
  try {
    await api("PUT", identityPath() + "/binding", {
      name: $("#identityName").value, email: $("#identityEmail").value.trim()
    });
    await loadIdentities();
    setResult($("#identityResult"), "Eşleştirme kaydedildi. Önceki rapor belgeleri varsa yeniden oluşturulmalı.", true);
  } catch (e) { setResult($("#identityResult"), e.message, false); }
  finally { button.disabled = false; }
});

const ROLE_LABELS = {admin: "Yönetici", operator: "Kullanıcı", viewer: "İzleyici"};

async function patchAccount(id, changes) {
  try { await api("PATCH", `/api/auth/accounts/${id}`, changes); }
  catch (e) { toast(e.message); }
  await loadAccounts();
}

async function openUserMeetings(account) {
  const data = await api("GET", `/api/admin/meetings?limit=100&account_id=${account.id}`);
  $("#userMeetingsTitle").textContent = `${account.email} · Toplantılar`;
  $("#userMeetings").replaceChildren(...data.meetings.map(m => el("li", {},
    el("button", {class: "small", onclick: () => openMeetingDetail(m.id)}, m.title || m.id),
    ` · ${fmtDate(m.started_at)} · ${m.minutes ?? "–"} dk`)));
  if (!data.meetings.length) $("#userMeetings").append(el("li", {}, "Toplantı yok."));
}

async function openMeetingDetail(id) {
  try {
    const base = `/api/meetings/${encodeURIComponent(id)}`;
    const transcript = await api("GET", base + "/transcript");
    $("#meetingDetailTitle").textContent = `Toplantı · ${id}`;
    $("#meetingDetailText").replaceChildren(...transcript.map(s => el("p", {}, `${s.speaker || s.speaker_label || "Katılımcı"}: ${s.text}`)));
    $("#meetingDetailLinks").replaceChildren(el("a", {href: `/panel/${encodeURIComponent(id)}`, target: "_blank", rel: "noopener"}, "Paneli aç"), " · ",
      el("a", {href: base + "/report.pdf", target: "_blank", rel: "noopener"}, "PDF"), " · ",
      el("a", {href: base + "/report.docx", target: "_blank", rel: "noopener"}, "DOCX"));
    $("#meetingDetail").showModal();
  } catch (e) { toast(e.message); }
}
$("#meetingDetailClose").addEventListener("click", () => $("#meetingDetail").close());

async function loadAccounts() {
  const accounts = await api("GET", "/api/auth/accounts");
  $("#panelAccounts").replaceChildren(...accounts.map(a => {
    const roles = el("select", {"aria-label": `${a.email} rolü`, onchange: ev => patchAccount(a.id, {role: ev.target.value})},
      ...Object.entries(ROLE_LABELS).map(([value, label]) => el("option", {value}, label)));
    roles.value = a.role;
    const enabled = el("input", {type: "checkbox", "aria-label": `${a.email} erişimi açık`, onchange: ev => patchAccount(a.id, {enabled: ev.target.checked})});
    enabled.checked = a.enabled;
    return el("tr", {},
      el("td", {}, el("button", {class: "small", onclick: () => openUserMeetings(a).catch(e => toast(e.message))}, a.email),
        a.active ? el("span", {class: "badge active"}, "Aktif") : ""),
      el("td", {}, roles), el("td", {}, enabled), el("td", {}, fmtDate(a.last_seen)),
      el("td", {}, a.active_sessions), el("td", {}, a.meetings_30d),
      el("td", {}, `${a.minutes_30d} dk`), el("td", {}, usd(a.cost_30d)),
      el("td", {}, el("button", {class: "small danger", onclick: async () => {
        if (!confirm(`${a.email} hesabı silinsin mi? Panel erişimi kapatılır; toplantı kayıtları korunur.`)) return;
        try { await api("DELETE", `/api/auth/accounts/${a.id}`); await loadAccounts(); }
        catch (e) { toast(e.message); }
      }}, "Sil")));
  }));
}
$("#identityReport").addEventListener("click", async (ev) => {
  ev.target.disabled = true;
  try {
    const path = identityPath().replace(/\/participants$/, "/report");
    await api("POST", path);
    setResult($("#identityResult"), "Rapor belgeleri güncel e-postalarla oluşturuldu.", true);
  } catch (e) { setResult($("#identityResult"), e.message, false); }
  finally { ev.target.disabled = false; }
});
$("#accountForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  ev.submitter.disabled = true;
  try {
    await api("POST", "/api/auth/accounts", {
      email: ev.target.elements.email.value.trim(), password: ev.target.elements.password.value,
      role: ev.target.elements.role.value
    });
    ev.target.reset();
    await loadAccounts();
    setResult($("#accountResult"), "Hesap kaydedildi; e-posta ve kişisel parolayla giriş yapılabilir.", true);
  } catch (e) { setResult($("#accountResult"), e.message, false); }
  finally { ev.target.elements.password.value = ""; ev.submitter.disabled = false; }
});

// ------------------------------------------------------------------ açılış
setInterval(() => {
  if (!$("#app").hidden && !document.hidden && !$("section[data-view=overview]").hidden) {
    loadOverview().catch(() => {});
  }
}, 10000);

(async () => {
  refreshLogo();
  try {
    await api("GET", "/api/auth/me");
    await showApp();
  } catch (e) {
    showLogin();
  }
})();
