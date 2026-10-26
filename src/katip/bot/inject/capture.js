/**
 * Sayfaya en erken anda (document_start) enjekte edilir.
 *
 * Görevi:
 *  1. RTCPeerConnection'ı sarmalayıp uzak ses track'lerini yakalamak,
 *  2. hepsini tek bir karışık akışta toplayıp 16 kHz mono int16 PCM olarak bot
 *     sürecine (Playwright köprüsü) akıtmak,
 *  3. DOM'dan katılımcı listesi + aktif konuşmacı bilgisini periyodik yayınlamak,
 *  4. toplantı chat'ini okuyup sunucuya iletmek ("/özet" gibi komutlar) ve sunucudan
 *     gelen {"kind": "chat_send"} mesajlarını chat'e yazmak,
 *  5. sesli cevap: sayfanın mikrofon isteğine (getUserMedia) sentetik bir mikrofon
 *     döndürüp sunucudan gelen TTS sesini ona çalmak. İşletim sistemi sanal mikrofonu
 *     ya da sürücü gerekmez — Windows, macOS ve Linux'ta aynı çalışır.
 *
 * Neden karışık tek akış: Meet uzak sesi 3 sanal track'te gönderir ve track'lerin
 * kaynağı konuşana göre değişir (track ≠ kişi). STT (Soniox) stream süresini
 * faturaladığı için tek akış hem doğru hem ucuz. Konuşmacıyı diarization ayırır.
 *
 * Taşıma: sayfa sunucuya kendisi bağlanmaz. Teams/Meet'in içerik politikası (CSP) ve
 * Chrome'un yerel ağ erişimi kısıtlaması, sayfadan 127.0.0.1'e WebSocket açılmasını
 * engelliyor (canlı Teams testinde görüldü). Bunun yerine:
 *   sayfa → bot : window.__katipRelay({t: "json", d: {...}}) ya da {t: "audio", d: base64}
 *   bot → sayfa : bot süreci window.__katipDeliver(mesaj) çağırır
 * Bot süreci (runner.py) sunucuya kendi WebSocket'ini açar; tarayıcı kuralları devre dışı.
 *
 * Ses paketi düzeni (little-endian, base64):
 *   uint32 trackId (hep 0 = karışık) | uint32 sequence | int16[] pcm
 *
 * window.__KATIP_CONFIG__ Python tarafından add_init_script ile önceden set edilir:
 *   { sessionId, sampleRate, selectors: { participant, activeSpeaker, name, chat…, mic… } }
 */
(() => {
  if (window.__katipInstalled) return;
  window.__katipInstalled = true;

  const CFG = window.__KATIP_CONFIG__ || {};
  const SAMPLE_RATE = CFG.sampleRate || 16000;
  const log = (...a) => console.log("[katip]", ...a);

  // ------------------------------------------------------------ bot köprüsü
  const pending = [];

  function relay(msg) {
    // Playwright bağlamayı init script'lerle aynı anda kurar; nadiren birkaç ms geç kalırsa
    // mesajlar kısa bir kuyrukta bekler.
    if (typeof window.__katipRelay === "function") {
      while (pending.length) window.__katipRelay(pending.shift()).catch(() => {});
      window.__katipRelay(msg).catch(() => {});
      return true;
    }
    return false;
  }

  function sendJson(obj) {
    if (!relay({ t: "json", d: obj }) && pending.length < 200) pending.push({ t: "json", d: obj });
  }

  function toBase64(buf) {
    const bytes = new Uint8Array(buf);
    let s = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(s);
  }

  function sendAudio(buf) {
    // Ses gerçek zamanlıdır: köprü hazır değilse paket düşer, birikmez.
    if (typeof window.__katipRelay === "function") {
      window.__katipRelay({ t: "audio", d: toBase64(buf) }).catch(() => {});
    }
  }

  // Bot sürecinden gelen mesajlar (chat'e yaz, konuş…).
  window.__katipDeliver = (msg) => {
    if (!msg || !msg.kind) return;
    if (msg.kind === "chat_send" && msg.text) sendChat(msg.text);
    else if (msg.kind === "chat_mark" && msg.text) markSent(msg.text);
    else if (msg.kind.startsWith("speak_")) onSpeak(msg);
  };

  window.__katipSendJson = sendJson;

  // ----------------------------------------------------------- audio graph
  const MIX_TRACK_ID = 0;
  let audioCtx = null;
  let mixBus = null;
  let nextTrackId = 1;
  const seenTracks = new WeakMap();
  // Uzak track'lerin ses öğeleri ve kaynak düğümleri burada GÜÇLÜ referansla tutulur.
  // Tutulmazsa çöp toplayıcı ~1 dakika sonra <audio> öğesini siler ve Chromium'da uzak ses
  // Web Audio'ya akmayı bırakır: sunucuya sessizlik gider (canlı Teams testinde görüldü).
  const liveTracks = new Map();

  function floatToPcm16(input, seq) {
    const out = new ArrayBuffer(8 + input.length * 2);
    const view = new DataView(out);
    view.setUint32(0, MIX_TRACK_ID, true);
    view.setUint32(4, seq, true);
    for (let i = 0; i < input.length; i++) {
      const s = Math.max(-1, Math.min(1, input[i]));
      view.setInt16(8 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return out;
  }

  /** Karışım hattını ilk uzak track geldiğinde kurar (öncesinde fatura/trafik yok). */
  function ensureMixer() {
    if (mixBus) return mixBus;
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: SAMPLE_RATE,
    });
    if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});

    mixBus = audioCtx.createGain();
    mixBus.gain.value = 1.0;
    // Birden çok konuşmacı üst üste binince kırpılmayı önler.
    const limiter = audioCtx.createDynamicsCompressor();
    limiter.threshold.value = -6;
    limiter.ratio.value = 12;

    // AudioWorklet CSP nedeniyle bloklanabiliyor; ScriptProcessor her yerde çalışıyor.
    const tap = audioCtx.createScriptProcessor(1024, 1, 1);
    let seq = 0;
    let peak = 0;
    let packets = 0;
    tap.onaudioprocess = (ev) => {
      const data = ev.inputBuffer.getChannelData(0);
      for (let i = 0; i < data.length; i += 16) peak = Math.max(peak, Math.abs(data[i]));
      packets++;
      sendAudio(floatToPcm16(data, seq++));
    };
    // Sağlık sinyali: sunucu loglarında sesin gerçekten akıp akmadığı görünsün.
    setInterval(() => {
      sendJson({
        kind: "audio_health", packets, peak: Math.round(peak * 1000) / 1000,
        tracks: liveTracks.size, ctx: audioCtx ? audioCtx.state : "none",
      });
      if (audioCtx && audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
      peak = 0;
      packets = 0;
    }, 5000);
    // Çıkış bağlanmazsa ScriptProcessor tetiklenmiyor; sessiz bir gain'e bağlıyoruz.
    const mute = audioCtx.createGain();
    mute.gain.value = 0;
    mixBus.connect(limiter);
    limiter.connect(tap);
    tap.connect(mute);
    mute.connect(audioCtx.destination);
    log("karışım hattı kuruldu");
    return mixBus;
  }

  function attachTrack(track, stream) {
    if (track.kind !== "audio" || seenTracks.has(track)) return;
    const trackId = nextTrackId++;
    seenTracks.set(track, trackId);

    // Chromium'da uzak track, bir media element'e bağlanmadan Web Audio'ya akmaz.
    const sink = new Audio();
    sink.srcObject = new MediaStream([track]);
    sink.muted = true;
    sink.play().catch(() => {});
    // Sayfaya da ekle: DOM'daki öğe hem referans tutulur hem autoplay/arka plan
    // kısıtlamalarına daha az takılır. Görünmez ve sessiz.
    sink.style.display = "none";
    (document.body || document.documentElement).appendChild(sink);

    const bus = ensureMixer();
    const src = audioCtx.createMediaStreamSource(new MediaStream([track]));
    src.connect(bus);

    sendJson({
      kind: "track_opened",
      track_id: trackId,
      label: track.label || null,
      stream_id: stream ? stream.id : null,
    });
    liveTracks.set(trackId, { track, sink, src });
    track.addEventListener("ended", () => {
      try { src.disconnect(); } catch (e) { /* zaten kopmuş */ }
      try { sink.srcObject = null; sink.remove(); } catch (e) { /* zaten gitmiş */ }
      liveTracks.delete(trackId);
      sendJson({ kind: "track_closed", track_id: trackId });
    });
    log("track karışıma eklendi", trackId, track.label);
  }

  // ------------------------------------------------------- RTCPeerConnection
  const NativePC = window.RTCPeerConnection;
  if (NativePC) {
    function PatchedPC(...args) {
      const pc = new NativePC(...args);
      pc.addEventListener("track", (ev) => {
        try {
          attachTrack(ev.track, ev.streams && ev.streams[0]);
        } catch (e) {
          log("attachTrack hatası", e);
        }
      });
      return pc;
    }
    PatchedPC.prototype = NativePC.prototype;
    for (const k of Object.keys(NativePC)) PatchedPC[k] = NativePC[k];
    window.RTCPeerConnection = PatchedPC;
    window.webkitRTCPeerConnection = PatchedPC;
    log("RTCPeerConnection sarmalandı");
  }

  // ------------------------------------------------------------- DOM roster
  const sel = CFG.selectors || {};
  let lastRoster = "";
  let lastRosterAt = 0;

  // Konuşma çerçevesi görünür mü? (Teams'te öğe hep DOM'da; konuşurken kenarlık/gölge alır.)
  const styleSeen = new Map();
  let styleReports = 0;
  function styleSignature(el) {
    const part = (cs) => [cs.borderTopWidth, cs.borderTopColor, cs.boxShadow, cs.outlineStyle,
                          cs.opacity, cs.visibility, cs.display, cs.backgroundColor].join("|");
    // Çerçeve sözde öğeyle (::before/::after) de çizilebilir: onları da imzaya kat.
    return [part(getComputedStyle(el)), part(getComputedStyle(el, "::before")),
            part(getComputedStyle(el, "::after"))].join(" / ");
  }
  // Teams çerçeveyi göstergenin ::before sözde öğesiyle çiziyor: konuşurken 4 px renkli
  // kenarlık ve opaklık 1, susunca şeffaf (7. test logu). Öğenin kendisi ve sözde öğeleri denenir.
  function visiblyActive(el) {
    return [null, "::before", "::after"].some((pseudo) => borderVisible(getComputedStyle(el, pseudo)));
  }
  function borderVisible(cs) {
    if (cs.display === "none" || cs.visibility === "hidden" || parseFloat(cs.opacity) < 0.05) return false;
    const transparent = (c) => !c || c === "transparent" || /rgba\([^)]*,\s*0\)$/.test(c);
    const border = parseFloat(cs.borderTopWidth) > 0 && cs.borderTopStyle !== "none" && !transparent(cs.borderTopColor);
    const shadow = cs.boxShadow && cs.boxShadow !== "none";
    const outline = cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0 && !transparent(cs.outlineColor);
    return Boolean(border || shadow || outline);
  }

  function readRoster() {
    if (!sel.participant) return;
    const nodes = document.querySelectorAll(sel.participant);
    const participants = [];
    const active = [];
    nodes.forEach((n) => {
      let name = "";
      if (sel.nameAttr) name = (n.getAttribute(sel.nameAttr) || "").trim();
      if (!name) {
        const nameEl = sel.name ? n.querySelector(sel.name) : n;
        name = (nameEl && nameEl.textContent || "").trim();
      }
      if (!name || participants.includes(name)) return;
      participants.push(name);
      if (!sel.activeSpeaker) return;
      const root = (sel.tileRoot && n.closest(sel.tileRoot)) || n;
      const indicator = root.querySelector(sel.activeSpeaker);
      if (!indicator) return;
      if (!sel.activeByStyle) { active.push(name); return; }
      if (visiblyActive(indicator)) active.push(name);
      // Kalibrasyon: göstergenin stil değişimlerini sunucu loguna bildir (ilk 300 değişim).
      const sig = styleSignature(indicator);
      if (styleSeen.get(name) !== sig && styleReports < 300) {
        styleSeen.set(name, sig);
        styleReports += 1;
        sendJson({ kind: "speaker_style", name, sig: maskNames(sig), active: visiblyActive(indicator) });
      }
    });
    lastNames = participants;
    const key = participants.join("|") + "#" + active.join("|");
    // Heartbeat lets the server expire stale speaker indicators after a DOM failure.
    const now = performance.now();
    if (key === lastRoster && now - lastRosterAt < 1000) return;
    lastRoster = key;
    lastRosterAt = now;
    sendJson({ kind: "roster", participants, active_speakers: active });
  }
  // Kalibrasyon: "konuşuyor" göstergesi stil değil sınıf/öznitelik değişimiyle de gösterilebilir.
  // Kutucuklardaki öznitelik değişimlerini (her farklı değişimi bir kez, en fazla 400) sunucuya
  // bildir; katılımcı adları <AD> ile maskelenir (loglarda kişisel veri olmasın).
  const domSeen = new Set();
  let domReports = 0;
  function maskNames(text) {
    let out = text || "";
    for (const n of lastNames) if (n) out = out.split(n).join("<AD>");
    return out.slice(0, 160);
  }
  let lastNames = [];
  function describeEl(el) {
    const tid = el.getAttribute && el.getAttribute("data-tid");
    if (tid && lastNames.includes(tid)) return "TILE";
    return tid || (el.tagName || "?").toLowerCase();
  }
  function startTileObserver() {
    if (!sel.tileRoot || !document.body) return;
    new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (domReports >= 400) return;
        const el = m.target;
        if (!el.closest || !el.closest(sel.tileRoot) || !el.closest(sel.tileRoot).querySelector(sel.participant)) continue;
        let detail;
        if (m.attributeName === "class") {
          const before = new Set((m.oldValue || "").split(/\s+/));
          const after = new Set((el.getAttribute("class") || "").split(/\s+/));
          const added = [...after].filter((c) => c && !before.has(c));
          const removed = [...before].filter((c) => c && !after.has(c));
          detail = "+" + added.join(",") + " -" + removed.join(",");
        } else {
          detail = maskNames(el.getAttribute(m.attributeName));
        }
        const key = describeEl(el) + "|" + m.attributeName + "|" + detail;
        if (domSeen.has(key)) continue;
        domSeen.add(key);
        domReports += 1;
        sendJson({ kind: "speaker_dom", el: describeEl(el), attr: m.attributeName, detail });
      }
    }).observe(document.body, {
      attributes: true, subtree: true, attributeOldValue: true,
      attributeFilter: ["class", "style", "aria-label", "data-is-speaking", "data-state", "data-active"],
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startTileObserver);
  else startTileObserver();

  setInterval(() => {
    try {
      readRoster();
    } catch (e) {
      /* DOM değişti, sessiz geç */
    }
  }, 250);


  // ------------------------------------------------------------ sesli cevap
  // Toplantı sayfası mikrofon istediğinde gerçek aygıt yerine bu sentetik akışı alır.
  let micCtx = null;
  let micDest = null;
  let playHead = 0;
  let sources = [];
  let speaking = false;
  let muteTimer = null;

  function ensureMic() {
    if (!micDest) {
      micCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 });
      micDest = micCtx.createMediaStreamDestination();
    }
    if (micCtx.state === "suspended") micCtx.resume().catch(() => {});
    return micDest;
  }

  const md = navigator.mediaDevices;
  if (md && md.getUserMedia) {
    const origGUM = md.getUserMedia.bind(md);
    md.getUserMedia = async (constraints) => {
      if (!constraints || !constraints.audio) return origGUM(constraints);
      const audio = ensureMic().stream.getAudioTracks().map((t) => t.clone());
      let video = [];
      if (constraints.video) {
        try { video = (await origGUM({ video: constraints.video })).getVideoTracks(); } catch (e) { /* kamera yok */ }
      }
      return new MediaStream([...audio, ...video]);
    };
    if (md.enumerateDevices) {
      const origEnum = md.enumerateDevices.bind(md);
      // Sunucuda gerçek mikrofon yoksa platform "mikrofon bulunamadı" demesin.
      md.enumerateDevices = async () => {
        const list = await origEnum().catch(() => []);
        if (list.some((d) => d.kind === "audioinput" && d.deviceId)) return list;
        const fake = { deviceId: "katip-mic", kind: "audioinput", label: "Katip", groupId: "katip" };
        fake.toJSON = () => ({ ...fake });
        return [...list, fake];
      };
    }
    log("sentetik mikrofon hazır");
  }

  function setMic(on) {
    const btn = q(on ? sel.micUnmute : sel.micMute);
    if (btn) btn.click();
    else log("mikrofon düğmesi bulunamadı", on);
  }

  function stopSpeaking() {
    clearTimeout(muteTimer);
    sources.forEach((s) => { try { s.stop(); } catch (e) { /* zaten bitti */ } });
    sources = [];
    playHead = 0;
    if (speaking) { speaking = false; setMic(false); }
  }

  function playChunk(b64, rate) {
    const bin = atob(b64);
    const n = bin.length >> 1;
    if (!n) return;
    const buf = micCtx.createBuffer(1, n, rate);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < n; i++) {
      let v = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8);
      if (v >= 0x8000) v -= 0x10000;
      ch[i] = v / 32768;
    }
    const src = micCtx.createBufferSource();
    src.buffer = buf;
    src.connect(micDest);
    const at = Math.max(playHead, micCtx.currentTime);
    src.start(at);
    playHead = at + buf.duration;
    sources.push(src);
    src.onended = () => { sources = sources.filter((s) => s !== src); };
  }

  function onSpeak(msg) {
    if (!micDest) return;  // bu çerçeve mikrofon istemedi: çağrı başka çerçevede
    if (msg.kind === "speak_start") {
      clearTimeout(muteTimer);
      ensureMic();
      if (!speaking) { speaking = true; setMic(true); }
      // Mikrofonun açılması ve karşı tarafa akışın başlaması için kısa pay.
      playHead = Math.max(playHead, micCtx.currentTime + 0.3);
    } else if (msg.kind === "speak_chunk" && micCtx) {
      playChunk(msg.pcm, msg.sample_rate);
    } else if (msg.kind === "speak_end" && micCtx) {
      const waitMs = Math.max(0, playHead - micCtx.currentTime) * 1000 + 400;
      muteTimer = setTimeout(() => { if (speaking) { speaking = false; setMic(false); } }, waitMs);
    } else if (msg.kind === "speak_stop") {
      stopSpeaking();
    }
  }

  // ------------------------------------------------------------------ chat
  // Botun kendi yazdıklarını geri okuyup kendine cevap vermemesi için.
  const recentlySent = [];
  const seenMessages = new WeakSet();
  const seenMessageKeys = new Set();
  const chatKey = (s) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");

  function messageKey(node) {
    const body = q("[id^='content-']", node);
    return (body && body.id) || node.getAttribute("data-mid") || node.id;
  }

  function rememberMessage(node) {
    const key = messageKey(node);
    if (key) {
      if (seenMessageKeys.has(key)) return false;
      seenMessageKeys.add(key);
      if (seenMessageKeys.size > 1000) seenMessageKeys.delete(seenMessageKeys.values().next().value);
    } else if (seenMessages.has(node)) return false;
    seenMessages.add(node);
    return true;
  }

  function q(selector, root) {
    return selector ? (root || document).querySelector(selector) : null;
  }

  function setNativeValue(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function markSent(text) {
    // Bot süreci mesajı gerçek klavyeyle yazdı: kendi mesajımızı komut sanmamak için işaretle.
    const now = Date.now();
    while (recentlySent.length && now - recentlySent[0].at > 600000) recentlySent.shift();
    recentlySent.push({ key: chatKey(text), at: now });
  }

  async function sendChat(text) {
    let input = q(sel.chatInput);
    if (!input && sel.chatToggle) {
      const toggle = q(sel.chatToggle);
      if (toggle) toggle.click();
      await new Promise((r) => setTimeout(r, 800));
      input = q(sel.chatInput);
    }
    if (!input) {
      log("chat girişi bulunamadı");
      sendJson({ kind: "status_detail", detail: "chat_input_not_found" });
      return;
    }
    markSent(text);

    input.focus();
    if (input.isContentEditable) {
      if (document.activeElement !== input && !input.contains(document.activeElement)) {
        log("chat alanına odaklanılamadı");
        return;
      }
      // Yalnızca alanın içeriğini seç (document.execCommand("selectAll") odak kaçarsa tüm
      // sayfayı seçiyordu — canlı Teams testinde görüldü).
      const range = document.createRange();
      range.selectNodeContents(input);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      document.execCommand("insertText", false, text);
    } else {
      setNativeValue(input, text);
    }
    await new Promise((r) => setTimeout(r, 150));
    const send = q(sel.chatSend);
    if (send && !send.disabled) {
      send.click();
    } else {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
    }
  }

  function readMessage(node) {
    const textEl = q(sel.chatText, node) || node;
    const text = (textEl.textContent || "").trim();
    if (!text) return;
    if (!rememberMessage(node)) return;
    if (sel.chatOwnMessage && node.closest(sel.chatOwnMessage)) return;
    const flat = chatKey(text);
    if (recentlySent.some((s) => {
      if (Date.now() - s.at > 600000 || !s.key || !flat) return false;
      const shorter = s.key.length < flat.length ? s.key : flat;
      const longer = s.key.length < flat.length ? flat : s.key;
      return flat === s.key || (shorter.length >= 20 && longer.includes(shorter));
    })) return;
    const authorEl = q(sel.chatAuthor, node);
    const author = (authorEl && authorEl.textContent || "").trim();
    sendJson({ kind: "chat", author, text });
  }

  function scanChat(root) {
    if (!sel.chatMessage) return;
    if (root.matches && root.matches(sel.chatMessage)) readMessage(root);
    if (root.querySelectorAll) root.querySelectorAll(sel.chatMessage).forEach(readMessage);
  }

  function startChatObserver() {
    if (!sel.chatMessage || !document.body) return;
    // Sayfa açıldığında zaten var olan mesajları komut sanmamak için işaretle.
    document.querySelectorAll(sel.chatMessage).forEach(rememberMessage);
    new MutationObserver((mutations) => {
      for (const m of mutations) m.addedNodes.forEach((n) => n.nodeType === 1 && scanChat(n));
    }).observe(document.body, { childList: true, subtree: true });
    log("chat izleniyor");
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startChatObserver);
  } else {
    startChatObserver();
  }
})();
