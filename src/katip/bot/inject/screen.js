(() => {
  if (window.__katipScreen || !navigator.mediaDevices) return;
  const canvas = document.createElement("canvas");
  canvas.width = 1280;
  canvas.height = 720;
  const ctx = canvas.getContext("2d", { alpha: false });
  const tracks = new Set();
  let image = null;
  let timer = null;
  let enabled = false;
  function paint() {
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, 1280, 720);
    if (image) {
      const scale = Math.min(1280 / image.width, 720 / image.height);
      const w = image.width * scale, h = image.height * scale;
      ctx.drawImage(image, (1280 - w) / 2, (720 - h) / 2, w, h);
    }
  }
  window.__katipScreen = {
    async show(url) {
      if (!/^(data:image\/png;base64,|blob:)/.test(url)) throw new Error("Geçersiz slayt");
      const next = new Image();
      next.src = url;
      await next.decode();
      image = next;
      enabled = true;
      paint();
      if (!timer) timer = setInterval(paint, 500);
    },
    stop() {
      enabled = false;
      for (const track of tracks) {
        track.stop();
        track.dispatchEvent(new Event("ended"));
      }
      tracks.clear();
      clearInterval(timer);
      timer = null;
      image = null;
      paint();
    },
    get active() { return [...tracks].some(t => t.readyState === "live"); }
  };
  const display = async () => {
    if (!enabled) throw new DOMException("Sunum başlatılmadı", "NotAllowedError");
    paint();
    const stream = canvas.captureStream(5);
    function register(track) {
      tracks.add(track);
      track.contentHint = "detail";
      const settings = track.getSettings.bind(track);
      track.getSettings = () => ({ ...settings(), width: 1280, height: 720,
        frameRate: 5, displaySurface: "monitor", logicalSurface: true, cursor: "never" });
      const clone = track.clone.bind(track);
      track.clone = () => register(clone());
      track.addEventListener("ended", () => tracks.delete(track));
      return track;
    }
    register(stream.getVideoTracks()[0]);
    return stream;
  };
  Object.defineProperty(navigator.mediaDevices, "getDisplayMedia", {
    value: display, configurable: true, writable: true
  });
  paint();
})();
