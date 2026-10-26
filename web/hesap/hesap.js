"use strict";
// Yetki kararı sunucudadır; panel yalnız izin verilen işlemleri gösterir.
const $ = s => document.querySelector(s);
const ROLES = {admin: "Yönetici", operator: "Kullanıcı", viewer: "İzleyici"};
const STATES = {prepared: "hazırlık", pending: "katılıyor", joining: "katılıyor", waiting_admission: "lobide bekliyor", active: "canlı", ended: "bitti", failed: "hata", error: "hata"};
let currentMeeting = null;
let detailVersion = 0;
let authVersion = 0;
let rows = [];

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  node.append(...children.map(c => c instanceof Node ? c : document.createTextNode(c ?? "")));
  return node;
}
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { $("#toast").hidden = true; }, 5000);
}
function showLogin() {
  authVersion++;
  detailVersion++;
  currentMeeting = null;
  rows = [];
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#detail").hidden = true;
  $("#meetings").replaceChildren();
  for (const id of ["#transcript", "#report", "#documents", "#downloads", "#meetingActions", "#shareLinks", "#usage"]) $(id).replaceChildren();
  $("#passwordForm").reset();
  $("#documentForm").reset();
}
async function api(method, path, body, raw) {
  const headers = {};
  if (method !== "GET") headers["X-CSRF-Token"] = decodeURIComponent(document.cookie.match(/(?:^|;\s*)katip_csrf=([^;]+)/)?.[1] || "");
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {method, headers, credentials: "same-origin", body: raw || (body !== undefined ? JSON.stringify(body) : undefined)});
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401) showLogin();
    const error = new Error(typeof data?.detail === "string" ? data.detail : `İşlem tamamlanamadı (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return data;
}
const date = value => value ? new Date(value).toLocaleString("tr-TR") : "–";
const money = value => "$" + Number(value || 0).toFixed(3);
const meetingPath = id => `/api/meetings/${encodeURIComponent(id)}`;
const documentPath = id => `/api/preparations/${encodeURIComponent(id)}/documents`;
const link = (href, label) => el("a", {href, class: "button", target: "_blank", rel: "noopener"}, label);
function action(label, callback) {
  return el("button", {type: "button", onclick: async ev => {
    ev.target.disabled = true;
    try { await callback(); } catch (error) { toast(error.message); }
    finally { ev.target.disabled = false; }
  }}, label);
}
async function load() {
  const version = authVersion;
  const [history, usage] = await Promise.all([api("GET", "/api/admin/meetings?limit=100"), api("GET", "/api/usage")]);
  if (version !== authVersion || $("#app").hidden) return;
  rows = history.meetings;
  $("#empty").hidden = rows.length > 0;
  $("#meetings").replaceChildren(...rows.map(m => el("tr", {},
    el("td", {}, action(m.title || m.id, () => detail(m))), el("td", {}, date(m.started_at)),
    el("td", {}, m.minutes == null ? "–" : `${m.minutes} dk`),
    el("td", {}, el("span", {class: "badge " + (m.state === "active" ? "active" : "")}, STATES[m.state] || m.state)))));
  const totals = usage.totals;
  $("#usage").replaceChildren(...[["Toplantı", totals.meetings], ["Dakika", totals.minutes], ["Maliyet", money(totals.usd.total)]].map(([label, value]) => el("div", {}, el("span", {class: "muted"}, label), el("strong", {class: "stat"}, value))));
}
async function showApp() {
  const version = ++authVersion;
  const me = await api("GET", "/api/auth/me");
  if (version !== authVersion) return;
  if (me.role === "admin") { location.replace("/admin"); return; }
  $("#identity").textContent = $("#accountIdentity").textContent = `${me.email} · ${ROLES[me.role]}`;
  $("#join").hidden = me.role !== "operator";
  $("#login").hidden = true;
  $("#app").hidden = false;
  await load();
}
async function optional(path) {
  try { return await api("GET", path); } catch (error) { if (error.status === 404) return null; throw error; }
}
async function detail(meeting) {
  const version = ++detailVersion;
  const base = meetingPath(meeting.id);
  const [transcript, report, documents] = await Promise.all([api("GET", base + "/transcript"), optional(base + "/report"), api("GET", documentPath(meeting.id))]);
  if (version !== detailVersion) return;
  currentMeeting = meeting;
  $("#detailTitle").textContent = meeting.title || meeting.id;
  $("#transcript").replaceChildren(...transcript.map(s => el("p", {}, `${s.speaker || s.speaker_label || "Katılımcı"}: ${s.text}`)));
  $("#report").replaceChildren();
  if (report) {
    const data = report.report;
    $("#report").append(el("h3", {}, data.title || "Toplantı raporu"), el("p", {}, data.summary || ""));
    for (const [key, label] of Object.entries({key_points: "Ana başlıklar", decisions: "Kararlar", action_items: "Yapılacak işler", open_questions: "Açık sorular", participants: "Katılımcı notları"})) {
      if (!data[key]?.length) continue;
      $("#report").append(el("h4", {}, label), el("ul", {}, ...data[key].map(item =>
        el("li", {}, typeof item === "string" ? item : [item.owner || item.name, item.text || item.task || item.highlights?.join("; "), item.due, item.priority].filter(Boolean).join(" · ")))));
    }
  } else $("#report").textContent = "Rapor henüz hazır değil.";
  $("#downloads").replaceChildren(...(report ? [link(base + "/report.pdf", "PDF indir"), link(base + "/report.docx", "DOCX indir")] : []));
  const active = ["pending", "joining", "waiting_admission", "active"].includes(meeting.state);
  $("#meetingActions").replaceChildren(...(active ? [link(`/panel/${encodeURIComponent(meeting.id)}`, "Canlı panel")] : []));
  if (meeting.can_manage) {
    if (active) $("#meetingActions").append(action("Katip'i toplantıdan çıkar", async () => {
      if (!confirm("Katip bu toplantıdan çıkarılsın mı?")) return;
      await api("DELETE", base); await load(); toast("Çıkış istendi.");
    }));
    $("#meetingActions").append(action("Raporu yeniden oluştur", async () => {
      await api("POST", base + "/report"); await detail(meeting);
    }), action("Paylaşım linki oluştur", async () => {
      const shared = await api("POST", base + "/share");
      if (version !== detailVersion) return;
      $("#shareLinks").replaceChildren(el("p", {}, `${shared.expires_days} gün geçerli; bağlantıyı bilenler okuyabilir.`), link(shared.panel_url, "Paylaşılabilir panel"), link(shared.report_pdf, "Paylaşılabilir PDF"), link(shared.report_docx, "Paylaşılabilir DOCX"));
    }));
  }
  $("#documents").replaceChildren(...documents.map(doc => el("li", {}, doc.name + (doc.presentation ? " · sunum" : ""), meeting.can_manage ? action("Sil", async () => {
    if (!confirm(`${doc.name} silinsin mi?`)) return;
    await api("DELETE", documentPath(meeting.id) + "/" + encodeURIComponent(doc.id)); await detail(meeting);
  }) : "")));
  $("#documentForm").hidden = !meeting.can_manage;
  $("#shareLinks").replaceChildren();
  $("#detail").hidden = false;
  $("#detail").scrollIntoView({behavior: "smooth", block: "start"});
}
function form(selector, callback) {
  $(selector).addEventListener("submit", async ev => {
    ev.preventDefault(); ev.submitter.disabled = true;
    try { await callback(); } catch (error) { toast(error.message); }
    finally { ev.submitter.disabled = false; }
  });
}
form("#loginForm", async () => {
  $("#loginError").textContent = "";
  try {
    await api("POST", "/api/auth/login", {email: $("#loginEmail").value.trim(), password: $("#loginPassword").value});
    await showApp();
  } catch (error) { $("#loginError").textContent = error.message; }
  finally { $("#loginPassword").value = ""; }
});
form("#joinForm", async () => {
  await api("POST", "/api/meetings", {url: $("#joinUrl").value, title: $("#joinTitle").value});
  $("#joinForm").reset(); await load(); toast("Katip toplantıya gönderildi.");
});
form("#documentForm", async () => {
  const meeting = currentMeeting;
  const file = $("#documentFile").files[0];
  await api("POST", documentPath(meeting.id) + `?filename=${encodeURIComponent(file.name)}&presentation=${$("#presentation").checked}`, undefined, file);
  $("#documentForm").reset(); await detail(meeting);
});
form("#passwordForm", async () => {
  await api("POST", "/api/auth/password", {current: $("#currentPassword").value, new: $("#newPassword").value});
  $("#passwordForm").reset(); showLogin(); toast("Parola değişti. Yeniden giriş yapın.");
});
$("#logout").addEventListener("click", async () => {
  try { await api("POST", "/api/auth/logout"); showLogin(); } catch (error) { toast(error.message); }
});
$("#refresh").addEventListener("click", () => load().catch(error => toast(error.message)));
$("#closeDetail").addEventListener("click", () => { detailVersion++; currentMeeting = null; $("#detail").hidden = true; });
showApp().catch(error => { showLogin(); if (error.status !== 401) toast(error.message); });
