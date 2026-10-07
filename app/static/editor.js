"use strict";
// FotoArchiv – Fotobearbeitung (nicht-destruktiv). Die Datei bleibt unverändert; die Einstellungen
// stehen im Katalog (items.edit). Dieselben Formeln stehen in app/edit.py – bei Änderungen BEIDE
// anpassen, sonst weicht das gespeicherte Ergebnis von der Vorschau ab.

const EDIT_GROUPS = [
  ["Licht", [["exposure", "Belichtung"], ["contrast", "Kontrast"], ["highlights", "Lichter"], ["shadows", "Tiefen"],
    ["whites", "Weiß"], ["blacks", "Schwarz"]]],
  ["Farbe", [["saturation", "Sättigung"], ["vibrance", "Dynamik"], ["temp", "Wärme"], ["tint", "Tönung"]]],
  ["Details", [["sharpen", "Schärfe", 0], ["vignette", "Vignette"]]],
];
const EDIT_ASPECTS = [["frei", "Frei"], ["orig", "Original"], ["1:1", "1:1"], ["4:3", "4:3"], ["3:2", "3:2"], ["16:9", "16:9"]];
const ed = { open: false };

function zoomForAngle(w, h, deg) {
  const t = Math.abs(deg) * Math.PI / 180, c = Math.cos(t), s = Math.sin(t);
  return Math.max((w * c + h * s) / w, (w * s + h * c) / h);
}

// Ton- und Farbregler auf RGBA-Pixel (wie edit.tone in Python)
// Mittlere Helligkeit je Rasterzelle (wie edit.lum_grid) – vor allen Änderungen am Bild
const EDIT_GRID = 16;
function editLumGrid(d, w, h) {
  const gx = Math.max(1, Math.round(EDIT_GRID * w / Math.max(w, h))), gy = Math.max(1, Math.round(EDIT_GRID * h / Math.max(w, h)));
  const xs = [], ys = [];
  for (let i = 0; i <= gx; i++) xs.push(i < gx ? Math.floor(i * w / gx) : w);
  for (let j = 0; j <= gy; j++) ys.push(j < gy ? Math.floor(j * h / gy) : h);
  const cx = new Int32Array(w), sums = new Float64Array(gx * gy);
  for (let i = 0, x = 0; x < w; x++) { while (x >= xs[i + 1]) i++; cx[x] = i; }
  for (let j = 0, y = 0, p = 0; y < h; y++) {
    while (y >= ys[j + 1]) j++;
    for (let x = 0; x < w; x++, p += 4) sums[j * gx + cx[x]] += (0.2126 * d[p] + 0.7152 * d[p + 1] + 0.0722 * d[p + 2]) / 255;
  }
  for (let j = 0; j < gy; j++) for (let i = 0; i < gx; i++) sums[j * gx + i] /= (xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j]);
  return { gx, gy, v: sums };
}
function editInterp(n, cells) {
  const i0 = new Int32Array(n), i1 = new Int32Array(n), t = new Float32Array(n);
  for (let k = 0; k < n; k++) {
    const f = (k + 0.5) / n * cells - 0.5, a = Math.min(Math.max(Math.floor(f), 0), cells - 1);
    i0[k] = a; i1[k] = Math.min(a + 1, cells - 1); t[k] = Math.min(Math.max(f - a, 0), 1);
  }
  return [i0, i1, t];
}
const editSmooth = (a, b, x) => { const t = Math.min(Math.max((x - a) / (b - a), 0), 1); return t * t * (3 - 2 * t); };

function editTone(d, w, h, e) {
  const g = k => (e[k] || 0) / 100;
  const bl = g("blacks"), wh = g("whites"), bp = -bl * 0.15, wp = 1 - wh * 0.15, lv = bl || wh, lr = Math.max(1e-3, wp - bp);
  const ex = e.exposure ? Math.pow(2, g("exposure") * 2) : 1, ct = 1 + g("contrast");
  const sh = g("shadows"), hl = g("highlights");
  let gb = null, gx = 0, R = null, C = null;
  if (sh || hl) {
    // Umgebungshelligkeit: Raster aus dem unbearbeiteten Bild, mit Weiß/Schwarz/Belichtung/Kontrast umgerechnet
    const G = editLumGrid(d, w, h);
    gx = G.gx;
    gb = G.v.map(v => {
      if (lv) v = (v - bp) / lr;
      v *= ex;
      if (ct !== 1) v = (v - 0.5) * ct + 0.5;
      return Math.min(1, Math.max(0, v));
    });
    R = editInterp(h, G.gy); C = editInterp(w, gx);
  }
  const t = g("temp"), ti = g("tint"), sat = g("saturation"), vib = g("vibrance"), vig = g("vignette");
  const cl = v => v < 0 ? 0 : v > 1 ? 1 : v;
  for (let y = 0, i = 0; y < h; y++) {
    const yy = (y + 0.5) / h - 0.5;
    for (let x = 0; x < w; x++, i += 4) {
      let r = d[i] / 255, gg = d[i + 1] / 255, b = d[i + 2] / 255;
      if (lv) { r = (r - bp) / lr; gg = (gg - bp) / lr; b = (b - bp) / lr; }
      if (ex !== 1) { r *= ex; gg *= ex; b *= ex; }
      if (ct !== 1) { r = (r - 0.5) * ct + 0.5; gg = (gg - 0.5) * ct + 0.5; b = (b - 0.5) * ct + 0.5; }
      if (sh || hl) {
        // nur die Helligkeit über eine Kurve ändern – Farbton und Sättigung bleiben
        const r0 = R[0][y], r1 = R[1][y], rt = R[2][y], c0 = C[0][x], c1 = C[1][x], ctt = C[2][x];
        const top = gb[r0 * gx + c0] * (1 - ctt) + gb[r0 * gx + c1] * ctt, bot = gb[r1 * gx + c0] * (1 - ctt) + gb[r1 * gx + c1] * ctt;
        const lb = top * (1 - rt) + bot * rt;
        const lum = Math.min(1, Math.max(1e-4, 0.2126 * r + 0.7152 * gg + 0.0722 * b));
        let ln = lum;
        if (sh) { const k = 1 + 1.5 * Math.abs(sh) * (1 - editSmooth(0, 0.6, lb)); ln = sh > 0 ? Math.pow(ln, 1 / k) : Math.pow(ln, k); }
        if (hl) { const k = 1 + 1.5 * Math.abs(hl) * editSmooth(0.4, 1, lb); ln = 1 - Math.pow(1 - ln, hl > 0 ? k : 1 / k); }
        // Helligkeit voll, Farbe beim Aufhellen nur mit der Wurzel des Faktors (wie edit.py)
        const f = ln / lum, cf = f > 1 ? Math.sqrt(f) : f;
        r = ln + (r - lum) * cf; gg = ln + (gg - lum) * cf; b = ln + (b - lum) * cf;
        // Kanal über 1: Sättigung zurücknehmen statt abschneiden (wie in edit.py)
        const m = Math.max(r, gg, b);
        if (m > 1) { const s = (1 - ln) / Math.max(m - ln, 1e-6); r = ln + (r - ln) * s; gg = ln + (gg - ln) * s; b = ln + (b - ln) * s; }
      }
      if (t || ti) { r *= 1 + t * 0.12; b *= 1 - t * 0.12; gg *= 1 - ti * 0.10; }
      if (sat || vib) {
        r = cl(r); gg = cl(gg); b = cl(b);
        const lum = 0.2126 * r + 0.7152 * gg + 0.0722 * b;
        let f = 1 + sat;
        if (vib) f *= 1 + vib * (1 - (Math.max(r, gg, b) - Math.min(r, gg, b)));
        r = lum + (r - lum) * f; gg = lum + (gg - lum) * f; b = lum + (b - lum) * f;
      }
      if (vig) {
        const xx = (x + 0.5) / w - 0.5, r2 = (xx * xx + yy * yy) / 0.5, m = 1 + vig * 0.8 * Math.pow(r2, 1.5);
        r *= m; gg *= m; b *= m;
      }
      d[i] = Math.round(cl(r) * 255); d[i + 1] = Math.round(cl(gg) * 255); d[i + 2] = Math.round(cl(b) * 255);
    }
  }
}
// Schärfe: Unscharf maskieren mit 3×3-Weichzeichner (Vorschau-Näherung von PIL UnsharpMask)
function editSharpen(d, w, h, amount) {
  const a = amount * 1.5 / 100, src = new Uint8ClampedArray(d);
  for (let y = 1; y < h - 1; y++) for (let x = 1; x < w - 1; x++) {
    const i = (y * w + x) * 4;
    for (let c = 0; c < 3; c++) {
      let s = 0;
      for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) s += src[i + (dy * w + dx) * 4 + c];
      const v = src[i + c], diff = v - s / 9;
      if (Math.abs(diff) > 2) d[i + c] = v + diff * a;
    }
  }
}

function editActive(e) {
  return !!e && Object.keys(e).some(k => k === "crop" ? !!e.crop : !!e[k]);
}

async function openEditor() {
  const it = viewer.item;
  if (!it || it.locked || it.kind === "video") return toast(it && it.kind === "video" ? "Videos werden im Videoschnitt bearbeitet" : "Nicht möglich");
  const img = new Image();
  img.src = `/edit-src/${it.id}?v=${Date.now()}`;
  Object.assign(ed, { open: true, it, img, e: JSON.parse(JSON.stringify(it.edit || {})), saved: JSON.stringify(it.edit || {}),
    cropMode: false, aspect: "frei", portrait: false, before: false, geo: null, drag: null });
  const el = document.createElement("div");
  el.id = "editor";
  el.innerHTML = `<div class="ed-bar"><button class="ed-cancel">Abbrechen</button><b>Bearbeiten</b><span class="muted ed-name"></span>
      <span style="flex:1"></span><button class="ed-before" title="Vorher/Nachher (Taste V)">Vorher</button>
      <button class="ed-auto" title="Belichtung und Kontrast automatisch">✨ Auto</button>
      <button class="ed-reset">Original</button><button class="primary ed-done">Fertig</button></div>
    <div class="ed-stage"><canvas class="ed-canvas"></canvas><div class="ed-load muted">Foto wird geladen …</div></div>
    <aside class="ed-panel">
      <h3>Zuschneiden &amp; Drehen</h3>
      <div class="row ed-aspects">${EDIT_ASPECTS.map(([k, l]) => `<button data-a="${k}">${l}</button>`).join("")}<button class="ed-port" title="Hoch-/Querformat tauschen">⇄</button></div>
      <div class="row"><button class="ed-crop">✂ Zuschneiden</button><button class="ed-rotl" title="90° links">⟲</button>
        <button class="ed-rotr" title="90° rechts">⟳</button><button class="ed-flip" title="Spiegeln">⇋ Spiegeln</button></div>
      <label class="ed-sl"><span>Begradigen <small data-v="angle"></small></span><input type="range" min="-45" max="45" step="0.1" data-k="angle"></label>
      ${EDIT_GROUPS.map(([title, list]) => `<h3>${title}</h3>` + list.map(([k, l, min]) =>
        `<label class="ed-sl"><span>${l} <small data-v="${k}"></small></span><input type="range" min="${min === 0 ? 0 : -100}" max="100" step="1" data-k="${k}"></label>`).join("")).join("")}
      <p class="muted" style="margin-top:14px">Die Originaldatei bleibt unverändert. Doppelklick auf einen Regler setzt ihn zurück.</p>
    </aside>`;
  document.body.appendChild(el);
  ed.el = el;
  ed.canvas = $(".ed-canvas", el);
  $(".ed-name", el).textContent = it.name;
  await new Promise((ok, bad) => { img.onload = ok; img.onerror = bad; }).catch(() => { toast("Foto konnte nicht geladen werden"); closeEditor(); });
  if (!ed.open) return;
  $(".ed-load", el).remove();
  bindEditor(el);
  syncSliders();
  editRender();
}

function closeEditor() {
  if (ed.el) ed.el.remove();
  ed.open = false;
  ed.el = null;
}

function bindEditor(el) {
  $$("input[data-k]", el).forEach(inp => {
    inp.oninput = () => { const v = +inp.value; if (v) ed.e[inp.dataset.k] = v; else delete ed.e[inp.dataset.k]; syncSliders(); queueRender(inp.dataset.k === "angle"); };
    inp.ondblclick = () => { delete ed.e[inp.dataset.k]; syncSliders(); queueRender(inp.dataset.k === "angle"); };
  });
  $(".ed-cancel", el).onclick = async () => {
    if (JSON.stringify(ed.e) !== ed.saved && !await confirmBox("Änderungen verwerfen?")) return;
    closeEditor();
  };
  $(".ed-done", el).onclick = saveEditor;
  $(".ed-reset", el).onclick = () => { ed.e = {}; ed.cropMode = false; syncSliders(); queueRender(true); };
  $(".ed-before", el).onclick = () => { ed.before = !ed.before; syncSliders(); queueRender(); };
  $(".ed-auto", el).onclick = () => { autoEdit(); syncSliders(); queueRender(); };
  $(".ed-crop", el).onclick = () => { ed.cropMode = !ed.cropMode; syncSliders(); queueRender(); };
  $(".ed-flip", el).onclick = () => { ed.e.flip ? delete ed.e.flip : (ed.e.flip = true); if (ed.e.crop) { const [x, y, w, h] = ed.e.crop; ed.e.crop = [1 - x - w, y, w, h]; } queueRender(true); syncSliders(); };
  $(".ed-rotl", el).onclick = () => rotateInEditor(false);
  $(".ed-rotr", el).onclick = () => rotateInEditor(true);
  $$("[data-a]", el).forEach(b => b.onclick = () => { ed.aspect = b.dataset.a; ed.cropMode = true; applyAspect(); syncSliders(); queueRender(); });
  $(".ed-port", el).onclick = () => { ed.portrait = !ed.portrait; ed.cropMode = true; applyAspect(); syncSliders(); queueRender(); };
  const c = ed.canvas;
  c.addEventListener("pointerdown", cropDown);
  c.addEventListener("pointermove", cropMove);
  c.addEventListener("pointerup", () => { ed.drag = null; });
  c.addEventListener("pointercancel", () => { ed.drag = null; });
}

function syncSliders() {
  const el = ed.el;
  if (!el) return;
  $$("input[data-k]", el).forEach(inp => {
    const v = ed.e[inp.dataset.k] || 0;
    inp.value = v;
    $(`[data-v="${inp.dataset.k}"]`, el).textContent = v ? (v > 0 ? "+" : "") + (inp.dataset.k === "angle" ? v.toFixed(1) + "°" : Math.round(v)) : "";
  });
  $(".ed-crop", el).classList.toggle("on", ed.cropMode);
  $(".ed-flip", el).classList.toggle("on", !!ed.e.flip);
  $(".ed-before", el).classList.toggle("on", ed.before);
  $(".ed-port", el).classList.toggle("on", ed.portrait);
  $$("[data-a]", el).forEach(b => b.classList.toggle("on", ed.cropMode && b.dataset.a === ed.aspect));
}

let edRaf = 0;
function queueRender(geoChanged) {
  if (geoChanged) ed.geo = null;
  cancelAnimationFrame(edRaf);
  edRaf = requestAnimationFrame(editRender);
}

// Begradigtes (und gespiegeltes) Bild in Vorschaugröße – wie edit.geometry ohne den Zuschnitt
function editGeo() {
  if (ed.geo) return ed.geo;
  const img = ed.img, stage = $(".ed-stage", ed.el).getBoundingClientRect();
  const s = Math.min(1, Math.max(stage.width, 600) * devicePixelRatio / img.naturalWidth, Math.max(stage.height, 400) * devicePixelRatio / img.naturalHeight);
  const w = Math.max(1, Math.round(img.naturalWidth * s)), h = Math.max(1, Math.round(img.naturalHeight * s));
  const g = document.createElement("canvas");
  g.width = w; g.height = h;
  const ctx = g.getContext("2d", { willReadFrequently: true });
  const ang = ed.e.angle || 0, z = ang ? zoomForAngle(w, h, ang) : 1;
  ctx.translate(w / 2, h / 2);
  ctx.rotate(ang * Math.PI / 180);
  ctx.scale(z * (ed.e.flip ? -1 : 1), z);
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(img, -w / 2, -h / 2, w, h);
  return (ed.geo = g);
}

function editRender() {
  if (!ed.open) return;
  const c = ed.canvas;
  if (ed.before) {
    c.width = ed.img.naturalWidth; c.height = ed.img.naturalHeight;
    c.getContext("2d").drawImage(ed.img, 0, 0);
    return;
  }
  const g = editGeo(), crop = ed.e.crop || [0, 0, 1, 1];
  const sx = ed.cropMode ? 0 : Math.round(crop[0] * g.width), sy = ed.cropMode ? 0 : Math.round(crop[1] * g.height);
  const sw = ed.cropMode ? g.width : Math.max(1, Math.round(crop[2] * g.width)), sh = ed.cropMode ? g.height : Math.max(1, Math.round(crop[3] * g.height));
  c.width = sw; c.height = sh;
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(g, sx, sy, sw, sh, 0, 0, sw, sh);
  const tone = Object.keys(ed.e).some(k => !["crop", "angle", "flip", "sharpen"].includes(k) && ed.e[k]);
  if (tone || ed.e.sharpen) {
    const id = ctx.getImageData(0, 0, sw, sh);
    if (tone) editTone(id.data, sw, sh, ed.e);
    if (ed.e.sharpen) editSharpen(id.data, sw, sh, ed.e.sharpen);
    ctx.putImageData(id, 0, 0);
  }
  if (ed.cropMode) drawCrop(ctx, sw, sh, crop);
}

function drawCrop(ctx, w, h, [x, y, cw, ch]) {
  const X = x * w, Y = y * h, W = cw * w, H = ch * h, lw = Math.max(1, w / 600);
  ctx.save();
  ctx.fillStyle = "rgba(0,0,0,.55)";
  ctx.beginPath(); ctx.rect(0, 0, w, h); ctx.rect(X, Y, W, H); ctx.fill("evenodd");
  ctx.strokeStyle = "rgba(255,255,255,.9)"; ctx.lineWidth = lw * 1.5; ctx.strokeRect(X, Y, W, H);
  ctx.strokeStyle = "rgba(255,255,255,.35)"; ctx.lineWidth = lw;
  for (let i = 1; i < 3; i++) {
    ctx.beginPath(); ctx.moveTo(X + W * i / 3, Y); ctx.lineTo(X + W * i / 3, Y + H); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(X, Y + H * i / 3); ctx.lineTo(X + W, Y + H * i / 3); ctx.stroke();
  }
  ctx.fillStyle = "#fff";
  const hs = lw * 10;
  for (const [px, py] of [[X, Y], [X + W, Y], [X, Y + H], [X + W, Y + H]]) ctx.fillRect(px - hs / 2, py - hs / 2, hs, hs);
  ctx.restore();
}

// Seitenverhältnis in Bildpunkten → Verhältnis der normierten Breite/Höhe
function aspectValue() {
  const g = editGeo();
  let r = { "1:1": 1, "4:3": 4 / 3, "3:2": 3 / 2, "16:9": 16 / 9 }[ed.aspect] || (ed.aspect === "orig" ? g.width / g.height : 0);
  if (r && ed.portrait) r = 1 / r;
  return r ? r * g.height / g.width : 0;
}
function applyAspect() {
  const r = aspectValue();
  if (!r) { if (!ed.e.crop) ed.e.crop = [0, 0, 1, 1]; return; }
  const [x, y, w, h] = ed.e.crop || [0, 0, 1, 1];
  const cx = x + w / 2, cy = y + h / 2;
  let nw = w, nh = nw / r;
  if (nh > h) { nh = h; nw = nh * r; }
  if (nw > 1) { nw = 1; nh = nw / r; }
  if (nh > 1) { nh = 1; nw = nh * r; }
  ed.e.crop = [Math.min(Math.max(0, cx - nw / 2), 1 - nw), Math.min(Math.max(0, cy - nh / 2), 1 - nh), nw, nh];
}

function canvasPos(ev) {
  const r = ed.canvas.getBoundingClientRect();
  // Leinwand ist per CSS eingepasst (object-fit: contain) – sichtbare Fläche berechnen
  const s = Math.min(r.width / ed.canvas.width, r.height / ed.canvas.height);
  const vw = ed.canvas.width * s, vh = ed.canvas.height * s;
  return [(ev.clientX - r.left - (r.width - vw) / 2) / vw, (ev.clientY - r.top - (r.height - vh) / 2) / vh, 12 / vw, 12 / vh];
}
function cropDown(ev) {
  if (!ed.cropMode || ed.before) return;
  const [px, py, tx, ty] = canvasPos(ev);
  const [x, y, w, h] = ed.e.crop || [0, 0, 1, 1];
  const near = (a, b, t) => Math.abs(a - b) <= t * 1.5;
  let mode = null;
  if (near(px, x, tx) && near(py, y, ty)) mode = "nw";
  else if (near(px, x + w, tx) && near(py, y, ty)) mode = "ne";
  else if (near(px, x, tx) && near(py, y + h, ty)) mode = "sw";
  else if (near(px, x + w, tx) && near(py, y + h, ty)) mode = "se";
  else if (near(py, y, ty) && px > x && px < x + w) mode = "n";
  else if (near(py, y + h, ty) && px > x && px < x + w) mode = "s";
  else if (near(px, x, tx) && py > y && py < y + h) mode = "w";
  else if (near(px, x + w, tx) && py > y && py < y + h) mode = "e";
  else if (px > x && px < x + w && py > y && py < y + h) mode = "move";
  else mode = "new";
  ed.drag = { mode, px, py, start: [x, y, w, h] };
  if (mode === "new") ed.drag.start = [px, py, 0, 0];
  ed.canvas.setPointerCapture(ev.pointerId);
}
function cropMove(ev) {
  if (!ed.drag) {
    if (!ed.cropMode) { ed.canvas.style.cursor = ""; return; }
    // Mauszeiger zeigt, was ein Ziehen an dieser Stelle tut
    const [px, py, tx, ty] = canvasPos(ev), [x, y, w, h] = ed.e.crop || [0, 0, 1, 1];
    const n = Math.abs(py - y) <= ty * 1.5, s = Math.abs(py - y - h) <= ty * 1.5;
    const wv = Math.abs(px - x) <= tx * 1.5, e = Math.abs(px - x - w) <= tx * 1.5;
    ed.canvas.style.cursor = (n && wv) || (s && e) ? "nwse-resize" : (n && e) || (s && wv) ? "nesw-resize"
      : n || s ? "ns-resize" : wv || e ? "ew-resize" : px > x && px < x + w && py > y && py < y + h ? "move" : "crosshair";
    return;
  }
  const [px, py] = canvasPos(ev);
  const d = ed.drag, [x, y, w, h] = d.start, cl = v => Math.min(1, Math.max(0, v));
  const r = aspectValue();
  let nx = x, ny = y, nw = w, nh = h;
  if (d.mode === "move") {
    nx = Math.min(Math.max(0, x + px - d.px), 1 - w);
    ny = Math.min(Math.max(0, y + py - d.py), 1 - h);
  } else if (d.mode.length === 1) {
    // Kante: nur diese Seite verschieben (bei festem Seitenverhältnis wächst die andere Richtung mittig mit)
    if (d.mode === "n") { ny = Math.min(cl(py), y + h - 0.02); nh = y + h - ny; }
    if (d.mode === "s") { nh = Math.max(0.02, cl(py) - y); }
    if (d.mode === "w") { nx = Math.min(cl(px), x + w - 0.02); nw = x + w - nx; }
    if (d.mode === "e") { nw = Math.max(0.02, cl(px) - x); }
    if (r) {
      if (d.mode === "n" || d.mode === "s") { nw = Math.min(1, nh * r); nx = cl(x + w / 2 - nw / 2); if (nx + nw > 1) nx = 1 - nw; }
      else { nh = Math.min(1, nw / r); ny = cl(y + h / 2 - nh / 2); if (ny + nh > 1) ny = 1 - nh; }
    }
  } else {
    // fester Gegenpunkt, gezogene Ecke folgt der Maus
    const ax = d.mode === "new" ? x : d.mode.includes("w") ? x + w : x;
    const ay = d.mode === "new" ? y : d.mode.includes("n") ? y + h : y;
    let bx = cl(px), by = cl(py);
    nw = Math.abs(bx - ax); nh = Math.abs(by - ay);
    if (r) {
      if (nw / nh > r) nw = nh * r; else nh = nw / r;
      bx = ax + (bx >= ax ? nw : -nw); by = ay + (by >= ay ? nh : -nh);
    }
    nx = Math.min(ax, bx); ny = Math.min(ay, by);
    if (nw < 0.02 || nh < 0.02) return;
  }
  ed.e.crop = [nx, ny, nw, nh];
  queueRender();
}

async function rotateInEditor(cw) {
  // gleiche Drehung wie im Betrachter (in FotoArchiv, Datei bleibt); der Server dreht den Zuschnitt mit
  await api(`/api/item/${ed.it.id}/edit`, { edit: ed.e });
  await api(`/api/item/${ed.it.id}/rotate`, { dir: cw ? "cw" : "ccw" });
  const it = await api("/api/item/" + ed.it.id);
  ed.it = it;
  ed.e = JSON.parse(JSON.stringify(it.edit || {}));
  ed.saved = JSON.stringify(ed.e);
  thumbVer[it.id] = Date.now();
  const img = new Image();
  img.src = `/edit-src/${it.id}?v=${Date.now()}`;
  await new Promise(ok => { img.onload = ok; img.onerror = ok; });
  ed.img = img;
  syncSliders();
  queueRender(true);
}

// Automatisch: Schwarz-/Weißpunkt auf 0,5 %/99,5 % der Helligkeiten, Mitte auf ~42 %, etwas Dynamik
function autoEdit() {
  const c = document.createElement("canvas"), s = Math.min(1, 256 / Math.max(ed.img.naturalWidth, ed.img.naturalHeight));
  c.width = Math.max(1, Math.round(ed.img.naturalWidth * s)); c.height = Math.max(1, Math.round(ed.img.naturalHeight * s));
  const ctx = c.getContext("2d");
  ctx.drawImage(ed.img, 0, 0, c.width, c.height);
  const d = ctx.getImageData(0, 0, c.width, c.height).data, lum = [];
  for (let i = 0; i < d.length; i += 4) lum.push((0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]) / 255);
  lum.sort((a, b) => a - b);
  const q = p => lum[Math.min(lum.length - 1, Math.floor(p * lum.length))];
  const lo = q(0.005), hi = q(0.995), med = q(0.5), clamp = v => Math.round(Math.max(-100, Math.min(100, v)));
  const blacks = clamp(-lo / 0.15 * 100), whites = clamp((1 - hi) / 0.15 * 100);
  const bp = -blacks / 100 * 0.15, wp = 1 - whites / 100 * 0.15, m2 = (med - bp) / Math.max(1e-3, wp - bp);
  const exposure = clamp(Math.log2(0.42 / Math.max(0.02, m2)) * 50 * 0.8);
  Object.assign(ed.e, { blacks, whites, exposure: Math.max(-60, Math.min(60, exposure)), vibrance: Math.max(ed.e.vibrance || 0, 15) });
  for (const k of ["blacks", "whites", "exposure"]) if (!ed.e[k]) delete ed.e[k];
}

async function saveEditor() {
  const e = editActive(ed.e) ? ed.e : null;
  if (e && e.crop && e.crop[0] < 0.001 && e.crop[1] < 0.001 && e.crop[2] > 0.999 && e.crop[3] > 0.999) delete e.crop;
  const btn = $(".ed-done", ed.el);
  btn.disabled = true;
  btn.textContent = "Speichert …";
  try {
    await api(`/api/item/${ed.it.id}/edit`, { edit: e });
  } catch (err) { btn.disabled = false; btn.textContent = "Fertig"; return toast(err.message); }
  thumbVer[ed.it.id] = Date.now();
  if (currentGrid) $$(".cell img", currentGrid.el).forEach(img => {
    if (+img.closest(".cell").dataset.i === currentGrid.ids.indexOf(ed.it.id)) img.src = thumbUrl(ed.it.id);
  });
  closeEditor();
  toast(e ? "Gespeichert – das Original bleibt unverändert" : "Original wiederhergestellt");
  showItem();
}

document.addEventListener("keydown", ev => {
  if (!ed.open || ev.target.matches("input[type=text], textarea")) return;
  if (ev.key === "Escape" && !$(".modal-bg")) { ev.stopImmediatePropagation(); $(".ed-cancel", ed.el).click(); }
  else if (ev.key === "v" || ev.key === "V") { ed.before = !ed.before; syncSliders(); queueRender(); }
  else if (ev.key === "Enter") saveEditor();
  else return;
  ev.preventDefault();
}, true);
window.addEventListener("resize", () => { if (ed.open) queueRender(true); });
