"use strict";
// FotoArchiv – Videoschnitt. Projekt = Schnittfolge aus Fotos/Videos (Reihenfolge = Ergebnis), Originalton und
// Musik darunter; gerendert wird lokal auf dem Server (app/video.py). Bedienung nach dem Vorbild SCI-MO
// (Renderliste in suche.html): Lineal mit Zeiten, Zoom, J/K/L, I/O, Rasierklinge.
// Die Zeitachse (vTimeline) rechnet wie video.timeline in Python – bei Änderungen beide anpassen.

const VFMT = { "16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1, "4:3": 4 / 3 };
const VDIRECT = ["mp4", "m4v", "mov", "qt", "webm"];
const VTRANS = [["cut", "Schnitt", "|"], ["fade", "Überblenden", "◐"], ["black", "über Schwarz", "■"]];
const VRASTER = [0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800];
const vs = { pid: null, p: null, items: {}, sel: { k: "clip", i: 0 }, t: 0, rate: 0, zoom: 1, saveTimer: null, jobsTimer: null };

const vClipLen = c => c.kind === "video" ? c.out - c.in : c.dur;
function vTimeline(d) {
  const out = [];
  let t = 0, prev = null;
  for (const c of d.clips) {
    const ln = vClipLen(c);
    let xin = 0, black = 0;
    if (prev !== null && c.trans === "fade") { xin = Math.min(c.tdur, prev / 2, ln / 2); t -= xin; }
    else if (prev !== null && c.trans === "black") black = Math.min(c.tdur, prev, ln);
    out.push({ start: t, len: ln, xin, black });
    t += ln;
    prev = ln;
  }
  return { tl: out, total: Math.max(0, t) };
}
const vTime = (s, fine) => {
  s = Math.max(0, s || 0);
  const m = Math.floor(s / 60), r = s - m * 60;
  return `${m}:${r < 10 ? "0" : ""}${r.toFixed(fine === false ? 0 : 1)}`;
};
// "1:02.5", "62.5", "1:02" – was man tippt
function vParse(text) {
  const m = /^\s*(?:(\d+):)?(\d+(?:[.,]\d+)?)\s*$/.exec(String(text || ""));
  return m ? (m[1] ? +m[1] * 60 : 0) + parseFloat(m[2].replace(",", ".")) : null;
}
const vColor = (id, a) => `hsl(${[8, 200, 145, 35, 275, 175, 315, 95][id % 8]} 60% 45% / ${a == null ? 1 : a})`;
const vFps = () => (vs.p && vs.p.fps) || 25;

// ------------------------------------------------------------ Übersicht ----
routes.video = async function (id) {
  clearMain();
  stopVideoEditor();
  if (id) return videoEditor(+id);
  const d = await api("/api/vprojects");
  const page = document.createElement("div");
  page.className = "page";
  page.innerHTML = `<h1>Videoschnitt <small>${d.projects.length} Projekte</small><span style="flex:1"></span><button class="primary" id="vnew">Neues Projekt</button></h1>
    <p class="muted" style="max-width:860px">Fotos und Videos über 🎬+ (oben rechts an jedem Bild) oder markieren › „🎬 Video“ in ein Projekt legen.
      Dort Reihenfolge, Ausschnitte, Übergänge, Originalton und Musik festlegen und rendern.
      Das Ergebnis (MP4) kommt in den Ordner „FotoArchiv Export“ auf dem Schreibtisch.</p>
    <div class="folders">${d.projects.map(p => `<a class="folder" href="#/video/${p.id}">
      ${p.cover ? `<img loading="lazy" src="/thumb/${p.cover}" alt="">` : `<div class="vempty">🎬</div>`}
      <div><b>${esc(p.name)}</b><small>${p.clips} Clips · ${vTime(p.seconds)}</small></div></a>`).join("") || '<span class="muted">Noch keine Projekte.</span>'}</div>
    <h2 style="margin-top:26px">Renderliste</h2><div id="vjobs"></div>`;
  main.appendChild(page);
  $("#vnew", page).onclick = async () => {
    const name = await askText("Name des Videos", "");
    if (name) { const r = await api("/api/vprojects", { name }); location.hash = "#/video/" + r.id; }
  };
  renderJobs($("#vjobs", page), d.jobs);
  pollJobs(null);
};

function renderJobs(box, jobs) {
  if (!box) return;
  box.innerHTML = jobs.length ? `<table class="vjobs">${jobs.map(j => `<tr data-j="${j.id}">
      <td><b>${esc(j.name)}</b><br><small class="muted">${esc(j.created)}</small></td>
      <td style="width:40%">${j.status === "läuft" || j.status === "wartet"
        ? `<div class="progress"><i style="width:${Math.round((j.progress || 0) * 100)}%"></i></div><small class="muted">${esc(j.phase || j.status)} · ${Math.round((j.progress || 0) * 100)} %</small>`
        : j.status === "fertig" ? `<span class="ok">✓ fertig</span> <small class="muted">${j.seconds ? `in ${Math.round(j.seconds)} s` : ""}</small>`
          : `<span class="bad">${esc(j.status)}</span> <small class="muted">${esc(j.error || "")}</small>`}</td>
      <td style="text-align:right">${j.status === "läuft" || j.status === "wartet" ? `<button data-cancel>Abbrechen</button>` : ""}
        ${j.exists ? `<a href="/vjob/${j.id}.mp4" target="_blank"><button>▶ Ansehen</button></a> <button data-reveal>Im Ordner zeigen</button>` : ""}</td></tr>`).join("")}</table>`
    : '<span class="muted">Noch nichts gerendert.</span>';
  $$("[data-cancel]", box).forEach(b => b.onclick = async () => { await api(`/api/vjobs/${b.closest("tr").dataset.j}/cancel`, {}); pollJobs(vs.pid, true); });
  $$("[data-reveal]", box).forEach(b => b.onclick = async () => { try { await api(`/api/vjobs/${b.closest("tr").dataset.j}/reveal`, {}); } catch (e) { toast(e.message); } });
}
function pollJobs(pid, now) {
  clearTimeout(vs.jobsTimer);
  const run = async () => {
    if (!$("#vjobs")) return;
    const jobs = await api("/api/vjobs" + (pid ? "?project=" + pid : "")).catch(() => null);
    if (!jobs || !$("#vjobs")) return;
    renderJobs($("#vjobs"), jobs);
    if (jobs.some(j => j.status === "läuft" || j.status === "wartet")) vs.jobsTimer = setTimeout(run, 1500);
  };
  vs.jobsTimer = setTimeout(run, now ? 0 : 1500);
}

// Ein Klick am Foto/Video: ins zuletzt benutzte Projekt (Shift oder noch keins: Projekt wählen)
async function quickAddVideo(ids, choose) {
  const d = await api("/api/vprojects");
  const cur = d.projects.find(p => p.id === +localStorageGet("videoProject"));
  if (choose || !cur) {
    const r = await addToVideo(ids, d);
    if (r) toastAction(`In „${r.name}“ übernommen – 🎬+ fügt ab jetzt dort ein`, "Öffnen", () => { location.hash = "#/video/" + r.id; });
    return;
  }
  const r = await api(`/api/vprojects/${cur.id}/add`, { ids });
  toastAction(r.added ? `+ ${cur.name} (${cur.clips + r.added} Clips)` : "Nicht möglich (privat?)", "Öffnen", () => { location.hash = "#/video/" + cur.id; });
}

// Auswahl → Projekt (aus der Markier-Leiste)
async function addToVideo(ids, list) {
  const d = list || await api("/api/vprojects");
  const r = await pickFrom(`${ids.length === 1 ? "In" : ids.length + " Fotos/Videos in"} welches Videoprojekt?`,
    d.projects.map(p => ({ id: p.id, label: p.name, note: `${p.clips} Clips` })), { allowNew: true, placeholder: "Projekt suchen oder neuen Namen eintippen …" });
  if (!r) return null;
  if (r.new) {
    const x = await api("/api/vprojects", { name: r.new, ids });
    localStorageSet("videoProject", x.id);
    return { id: x.id, name: r.new };
  }
  await api(`/api/vprojects/${r.id}/add`, { ids });
  localStorageSet("videoProject", r.id);
  return { id: r.id, name: r.label };
}

// ------------------------------------------------------------ Schnittplatz ----
async function videoEditor(pid) {
  const [pr, lib] = await Promise.all([api("/api/vprojects/" + pid), api("/api/music?dur=1").catch(() => ({ music: [] }))]);
  localStorageSet("videoProject", pid);
  Object.assign(vs, { pid, p: pr.data, items: pr.items, name: pr.name, sel: { k: "clip", i: 0 }, t: 0, rate: 0, zoom: 1,
    library: lib.music || [] });
  const page = document.createElement("div");
  page.className = "page vpage";
  page.innerHTML = `<div class="crumbs"><a href="#/video">Videoschnitt</a> › <span class="vname" title="Klicken zum Umbenennen">${esc(pr.name)}</span>
      <span style="flex:1"></span>
      <label class="muted">Format <select id="vfmt">${Object.keys(VFMT).map(f => `<option${f === vs.p.format ? " selected" : ""}>${f}</option>`).join("")}</select></label>
      <label class="muted">Bilder/s <select id="vfps"><option value="25"${vs.p.fps === 25 ? " selected" : ""}>25</option><option value="30"${vs.p.fps === 30 ? " selected" : ""}>30</option></select></label>
      <label class="muted"><input type="checkbox" id="vuhd"${vs.p.uhd ? " checked" : ""}> 4K</label>
      <button id="vdel" class="danger">Projekt löschen</button><button class="primary" id="vrender">Rendern ▶</button></div>
    <div class="vtop">
      <div class="vplayer"><div class="vscreen">
        <video class="va" playsinline preload="auto"></video><video class="vb" playsinline preload="auto"></video><img class="vimg" alt="">
        <div class="vwait" hidden></div>
        <div class="vprev" hidden><div class="vprevbar"><span>✨ <b>Gerenderte Effekt-Vorschau</b> <span id="vprevrange"></span>
            <small>zeigt Übergänge, Blenden und Ton – zum Film kommt sie nicht</small></span>
          <span><button id="vprevagain">↻ Nochmal</button><button class="primary" id="vprevx">✕ Vorschau schließen (zurück zur Live-Ansicht)</button></span></div>
          <video controls playsinline></video></div>
      </div></div>
      <div class="vinspect" id="vinspect"></div>
    </div>
    <div class="vtransport">
      <button data-t="home" title="an den Anfang (Pos1)">⏮</button>
      <button data-t="j" title="rückwärts (J) – mehrmals: schneller">◀◀</button>
      <button data-t="play" id="vplay" title="Start/Stop (K oder Leertaste)">▶</button>
      <button data-t="l" title="vorwärts (L) – mehrmals: schneller">▶▶</button>
      <span class="vrate" id="vrate">—</span>
      <button data-t="fb" title="ein Bild zurück (←)">◁</button><button data-t="ff" title="ein Bild vor (→)">▷</button>
      <button data-t="sb" title="eine Sekunde zurück (Shift+←)">−1 s</button><button data-t="sf" title="eine Sekunde vor (Shift+→)">+1 s</button>
      <span class="vsep"></span>
      <button data-t="i" title="Einstieg des Clips hierher (I)">I</button><button data-t="o" title="Ausstieg des Clips hierher (O)">O</button>
      <button data-t="cut" title="hier schneiden (B) – aus einem Clip werden zwei">✂</button>
      <button data-t="del" title="gewählten Clip entfernen (Entf)">✕</button>
      <span class="vsep"></span><span class="muted">Zoom</span>
      <button data-t="zo" title="rauszoomen (−)">−</button><button data-t="zi" title="reinzoomen (+)">+</button><button data-t="z0" title="alles zeigen (0)">alles</button>
      <span class="vclock" id="vclock">0:00.0 / 0:00.0</span>
      <span style="flex:1"></span>
      <button data-t="prev" id="vprevbtn" title="Rechnet 8 Sekunden ab dem Abspielkopf klein – so sieht man Übergänge, Blenden und den gemischten Ton, die die Live-Ansicht nicht zeigt. Der fertige Film entsteht erst mit „Rendern ▶“.">✨ Effekt-Vorschau (8 s ab hier)</button>
    </div>
    <div class="vtlbox"><div class="vheads" id="vheads"></div><div class="vscroll" id="vscroll"><div class="vtl" id="vtl"></div></div></div>
    <div class="vkeys">J/K/L rückwärts/stopp/vorwärts · Leertaste Start/Stop · ← → Bild · Shift+← → Sekunde · I O Ein-/Ausstieg · B schneiden ·
      Entf entfernen · + − 0 Zoom (oder Strg+Mausrad) · Übergang/Ton/Musik in der Zeitleiste anklicken oder ziehen</div>
    <h2>Clips <small class="muted" id="vsum"></small></h2>
    <ol class="vclips" id="vclips"></ol>
    <h2>Renderliste</h2><div id="vjobs"></div>`;
  main.appendChild(page);
  vs.page = page;
  vs.A = $(".va", page); vs.B = $(".vb", page); vs.img = $(".vimg", page);
  vs.music = [];
  bindVideoEditor(page);
  vRedraw();
  renderJobs($("#vjobs", page), pr.jobs);
  pollJobs(pid);
}

function stopVideoEditor() {
  vSetRate(0, true);
  [vs.A, vs.B].forEach(v => { if (v) { try { v.pause(); } catch (e) {} } });
  (vs.music || []).forEach(a => { try { a.pause(); } catch (e) {} });
  vs.music = [];
  vs.page = null;
  vs.prev = null;
}
window.addEventListener("hashchange", () => { if (!location.hash.startsWith("#/video/")) stopVideoEditor(); });

function vSave() {
  clearTimeout(vs.saveTimer);
  vs.saveTimer = setTimeout(() => api(`/api/vprojects/${vs.pid}`, { data: vs.p }).catch(e => toast("Nicht gespeichert: " + e.message)), 400);
}
const vLive = () => vs.page && document.body.contains(vs.page);

function bindVideoEditor(page) {
  $(".vname", page).onclick = async () => {
    const n = await askText("Projekt umbenennen", vs.name);
    if (n && n !== vs.name) { vs.name = n; $(".vname", page).textContent = n; api(`/api/vprojects/${vs.pid}`, { name: n }); }
  };
  $("#vfmt", page).onchange = e => { vs.p.format = e.target.value; vSave(); vRedraw(); };
  $("#vfps", page).onchange = e => { vs.p.fps = +e.target.value; vSave(); };
  $("#vuhd", page).onchange = e => { vs.p.uhd = e.target.checked; vSave(); };
  $("#vdel", page).onclick = async () => {
    if (!await confirmBox(`Projekt „${vs.name}“ löschen? Fotos, Videos und bereits gerenderte Filme bleiben erhalten.`)) return;
    await api(`/api/vprojects/${vs.pid}/delete`, {});
    location.hash = "#/video";
  };
  $("#vrender", page).onclick = async () => {
    clearTimeout(vs.saveTimer);
    await api(`/api/vprojects/${vs.pid}`, { data: vs.p });
    try { await api(`/api/vprojects/${vs.pid}/render`, {}); toast("In der Renderliste – du kannst weiterarbeiten"); pollJobs(vs.pid, true); }
    catch (e) { toast(e.message, 6000); }
  };
  $$("[data-t]", page).forEach(b => b.onclick = () => vTransport(b.dataset.t));
  $("#vprevx", page).onclick = () => vClosePreview();
  $("#vprevagain", page).onclick = () => { const v = $(".vprev video", page); v.currentTime = 0; v.play().catch(() => {}); };
  // Strg/Cmd + Rad zoomt um die Maus, Rad allein scrollt
  $("#vscroll", page).addEventListener("wheel", e => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    const r = $("#vtl", page).getBoundingClientRect();
    vZoom(vs.zoom * (e.deltaY < 0 ? 1.3 : 1 / 1.3), (e.clientX - r.left) / r.width * vTimeline(vs.p).total, e.clientX);
  }, { passive: false });
}

function vTransport(k, shift) {
  const fr = 1 / vFps();
  if (vs.prev && (k === "play" || k === "k")) {
    const v = $(".vprev video", vs.page);
    if (v.paused) { if (v.ended) v.currentTime = 0; v.play().catch(() => {}); } else v.pause();
    return;
  }
  if (k === "home") vSeek(0);
  else if (k === "play") vSetRate(vs.rate === 0 ? 1 : 0);
  else if (k === "j") vSetRate(vs.rate > 0 ? -1 : Math.max(-8, vs.rate === 0 ? -1 : vs.rate * 2));
  else if (k === "k") vSetRate(0);
  else if (k === "l") vSetRate(vs.rate < 0 ? 1 : Math.min(8, vs.rate === 0 ? 1 : vs.rate * 2));
  else if (k === "fb") { vSetRate(0); vSeek(vs.t - fr); }
  else if (k === "ff") { vSetRate(0); vSeek(vs.t + fr); }
  else if (k === "sb") { vSetRate(0); vSeek(vs.t - 1); }
  else if (k === "sf") { vSetRate(0); vSeek(vs.t + 1); }
  else if (k === "i" || k === "o") vMark(k);
  else if (k === "cut") vSplit();
  else if (k === "del") { if (vs.sel.k === "music") vRemoveMusic(vs.sel.i); else if (vs.p.clips[vs.sel.i]) vRemove(vs.sel.i); }
  else if (k === "zi") vZoom(vs.zoom * 1.6, vs.t);
  else if (k === "zo") vZoom(vs.zoom / 1.6, vs.t);
  else if (k === "z0") vZoom(1, 0);
  else if (k === "prev") vPreview(vs.t, 8);
}
document.addEventListener("keydown", e => {
  if (!vLive() || e.target.matches("input, textarea, select") || $(".modal-bg") || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  const map = { j: "j", k: "k", l: "l", i: "i", o: "o", b: "cut", s: "cut", delete: "del", backspace: "del", "+": "zi", "=": "zi", "-": "zo", "0": "z0", home: "home" };
  if (k === " ") vTransport("play");
  else if (k === "arrowleft") vTransport(e.shiftKey ? "sb" : "fb");
  else if (k === "arrowright") vTransport(e.shiftKey ? "sf" : "ff");
  else if (map[k]) vTransport(map[k]);
  else return;
  e.preventDefault();
});

// ------------------------------------------------------------ Zeitleiste ----
function vRedraw() {
  if (!vLive()) return;
  const page = vs.page;
  $(".vscreen", page).style.aspectRatio = String(VFMT[vs.p.format] || 16 / 9);
  if (vs.sel.k === "clip") vs.sel.i = Math.min(vs.sel.i, Math.max(0, vs.p.clips.length - 1));
  const { total } = vTimeline(vs.p);
  $("#vsum", page).textContent = vs.p.clips.length ? `${vs.p.clips.length} · ${vTime(total)} · Reihenfolge per Ziehen` : "";
  vDrawTimeline();
  vDrawClips();
  vInspector();
  vShowAt(vs.t, false);
}

const vMusicDur = m => { const x = vs.library.find(l => l.path === m.path); return x && x.duration ? x.duration : null; };
const vMusicLen = (m, total) => Math.max(0.5, Math.min((vMusicDur(m) || 36000) - m.offset, total - m.start));

function vDrawTimeline() {
  const page = vs.page, { tl, total } = vTimeline(vs.p), T = Math.max(total, 1);
  const heads = $("#vheads", page), lane = $("#vtl", page);
  lane.style.width = (100 * vs.zoom) + "%";
  const pct = t => (100 * t / T) + "%";
  const sel = (k, i) => vs.sel.k === k && vs.sel.i === i;
  // Kopfspalte
  heads.innerHTML = `<div class="vh vh-ruler"></div><div class="vh vh-v">🎞 Bild</div><div class="vh vh-a">🔊 Originalton</div>
    ${vs.p.music.map((m, i) => `<div class="vh vh-m">♪ Musik ${i + 1}</div>`).join("")}
    <div class="vh vh-add"><button class="primary" id="vmadd">♪ + Musik</button></div>`;
  $("#vmadd", heads).onclick = e => vMusicMenu(e.target);
  // Lineal: Striche mit Zeit etwa alle 90 Pixel
  const w = lane.getBoundingClientRect().width || 800;
  const step = VRASTER.find(s => s >= 90 * T / w) || 3600;
  let ruler = "";
  for (let t = 0; t <= T + 1e-6; t += step) ruler += `<div class="vtick" style="left:${pct(t)}">${vTime(t, step < 1)}</div>`;
  // Bildspur: Clips, Überblend-Bereiche, Übergangsmarken, Ein-/Ausblenden
  let vtrack = "", atrack = "";
  tl.forEach((s, i) => {
    const c = vs.p.clips[i], it = vs.items[c.item] || {};
    vtrack += `<div class="vblk${sel("clip", i) ? " sel" : ""}" data-clip="${i}" style="left:${pct(s.start)};width:${pct(s.len)};background:${vColor(c.item, sel("clip", i) ? .75 : .45)}"
      title="${esc(it.name || "")} · ${vTime(s.len)}"><b>${i + 1}</b> ${c.kind === "video" ? "▶" : "📷"} ${esc(it.name || "")}<small>${vTime(s.len)}</small></div>`;
    if (s.xin > 0) vtrack += `<div class="vxf" style="left:${pct(s.start)};width:${pct(s.xin)}"></div>`;
    if (i > 0) {
      const at = c.trans === "fade" ? s.start + s.xin / 2 : s.start;
      const tr = VTRANS.find(x => x[0] === c.trans);
      vtrack += `<div class="vtrm${sel("trans", i) ? " sel" : ""}" data-trans="${i}" style="left:${pct(at)}"
        title="Übergang ${i}→${i + 1}: ${tr[1]}${c.trans !== "cut" ? " " + c.tdur.toFixed(1) + " s" : ""} – klicken: ändern, ziehen: Dauer">${tr[2]}</div>`;
    }
    if (c.kind === "video") {
      const v = Math.min(100, c.vol / 1.5 * 100);
      atrack += `<div class="vablk${sel("clip", i) ? " sel" : ""}" data-aud="${i}" style="left:${pct(s.start)};width:${pct(s.len)}"
        title="Originalton Clip ${i + 1}: ${Math.round(c.vol * 100)} % – hoch/runter ziehen ändert die Lautstärke">
        <i style="height:${v}%"></i><span>${Math.round(c.vol * 100)} %</span></div>`;
    }
  });
  if (tl.length) {
    vtrack += `<div class="vtrm vfade${sel("fadein", 0) ? " sel" : ""}" data-fade="in" style="left:0" title="Film einblenden: ${(vs.p.fadein || 0).toFixed(1)} s">◢</div>`;
    vtrack += `<div class="vtrm vfade${sel("fadeout", 0) ? " sel" : ""}" data-fade="out" style="left:100%" title="Film ausblenden: ${(vs.p.fadeout || 0).toFixed(1)} s">◣</div>`;
  }
  const mtracks = vs.p.music.map((m, i) => {
    const ln = vMusicLen(m, T), fi = Math.min(100, m.fin / ln * 100), fo = Math.min(100, m.fout / ln * 100);
    return `<div class="vrow vtrack-m"><div class="vmblk${sel("music", i) ? " sel" : ""}" data-mus="${i}" style="left:${pct(m.start)};width:${pct(ln)};
      background:linear-gradient(90deg, rgba(90,169,255,.15), rgba(90,169,255,.55) ${fi}%, rgba(90,169,255,.55) ${100 - fo}%, rgba(90,169,255,.15))"
      title="${esc(m.name)} – ziehen verschiebt, klicken: Lautstärke/Blenden">♪ ${esc(m.name)} <small>${Math.round(m.vol * 100)} % · ${vTime(ln)}</small></div></div>`;
  }).join("");
  lane.innerHTML = `<div class="vrow vruler">${ruler}</div><div class="vrow vtrack-v">${vtrack || '<span class="vhint">Noch keine Clips – 🎬+ an einem Foto/Video</span>'}</div>
    <div class="vrow vtrack-a">${atrack || '<span class="vhint">Originalton erscheint hier bei Videoclips</span>'}</div>${mtracks}
    <div class="vrow vtrack-add"><span class="vhint">${vs.library.length ? "♪ + Musik (links) legt ein Lied an den Abspielkopf" : "♪ + Musik (links): Lied aus FotoArchiv\\Musik oder vom Computer"}</span></div>
    ${vs.prev ? `<div class="vprevband" style="left:${pct(vs.prev.t0)};width:${pct(vs.prev.t1 - vs.prev.t0)}" title="Gerenderte Effekt-Vorschau – hier klicken spielt sie ab"><span>✨ Vorschau</span></div>` : ""}
    <div class="vcursor" id="vcursor"></div><div class="vhover" id="vhover"><span></span></div>`;
  vBindTimeline(lane, T);
  vCursor();
}

function vBindTimeline(lane, T) {
  const toT = x => { const r = lane.getBoundingClientRect(); return Math.max(0, Math.min(T, (x - r.left) / r.width * T)); };
  const pxPerSec = () => lane.getBoundingClientRect().width / T;
  lane.onmousemove = e => {
    const h = $("#vhover", lane), r = lane.getBoundingClientRect();
    h.style.display = "block";
    h.style.left = (e.clientX - r.left) + "px";
    $("span", h).textContent = vTime(toT(e.clientX));
  };
  lane.onmouseleave = () => { $("#vhover", lane).style.display = "none"; };
  // Klick: an DIE STELLE springen; auf einem Clip zusätzlich auswählen
  lane.onclick = e => {
    if (vs.dragged) { vs.dragged = false; return; }
    const t = toT(e.clientX);
    if (vs.prev) {
      if (t >= vs.prev.t0 && t <= vs.prev.t1) {
        const v = $(".vprev video", vs.page);
        v.currentTime = Math.max(0, t - vs.prev.t0);
        v.play().catch(() => {});
        return;
      }
      vClosePreview();
    }
    const blk = e.target.closest("[data-clip],[data-aud]");
    if (blk) vs.sel = { k: "clip", i: +(blk.dataset.clip ?? blk.dataset.aud) };
    vSetRate(0);
    vSeek(t);
    vDrawTimeline(); vDrawClips(); vInspector();
  };
  // Ziehen: Übergangsdauer, Originalton-Lautstärke, Musik verschieben
  lane.onpointerdown = e => {
    const tr = e.target.closest("[data-trans]"), fd = e.target.closest("[data-fade]"), au = e.target.closest("[data-aud]"), mu = e.target.closest("[data-mus]");
    if (!tr && !fd && !au && !mu) return;
    e.preventDefault();
    const x0 = e.clientX, y0 = e.clientY, pps = pxPerSec();
    let moved = false;
    const c = tr ? vs.p.clips[+tr.dataset.trans] : au ? vs.p.clips[+au.dataset.aud] : null;
    const m = mu ? vs.p.music[+mu.dataset.mus] : null;
    const start = tr ? c.tdur : au ? c.vol : mu ? m.start : (fd.dataset.fade === "in" ? vs.p.fadein : vs.p.fadeout) || 0;
    if (tr) vs.sel = { k: "trans", i: +tr.dataset.trans };
    else if (fd) vs.sel = { k: fd.dataset.fade === "in" ? "fadein" : "fadeout", i: 0 };
    else if (au) vs.sel = { k: "clip", i: +au.dataset.aud };
    else vs.sel = { k: "music", i: +mu.dataset.mus };
    const move = ev => {
      const dx = ev.clientX - x0, dy = ev.clientY - y0;
      if (!moved && Math.abs(dx) < 3 && Math.abs(dy) < 3) return;
      moved = true;
      if (tr) { if (c.trans === "cut") c.trans = "fade"; c.tdur = Math.max(0.2, Math.min(3, Math.round((start + 2 * dx / pps) * 10) / 10)); }
      else if (fd) { const v = Math.max(0, Math.min(5, Math.round((start + (fd.dataset.fade === "in" ? dx : -dx) / pps) * 10) / 10)); vs.p[fd.dataset.fade === "in" ? "fadein" : "fadeout"] = v; }
      else if (au) c.vol = Math.max(0, Math.min(1.5, Math.round((start - dy / 80) * 20) / 20));
      else m.start = Math.max(0, Math.round((start + dx / pps) * 10) / 10);
      vDrawTimeline(); vInspector();
    };
    const up = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      if (moved) { vs.dragged = true; vSave(); vShowAt(vs.t, true); }
      else { vs.dragged = true; vDrawTimeline(); vDrawClips(); vInspector(); if (au || mu) vSeek(toT(x0)); }
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
  };
}

function vZoom(z, anchor, screenX) {
  const sc = $("#vscroll", vs.page);
  if (!sc) return;
  const before = vs.zoom;
  vs.zoom = Math.max(1, Math.min(300, z));
  if (vs.zoom === before) return;
  vDrawTimeline();
  const w = $("#vtl", vs.page).getBoundingClientRect().width, T = vTimeline(vs.p).total || 1, box = sc.getBoundingClientRect();
  const target = screenX == null ? box.width / 2 : screenX - box.left;
  sc.scrollLeft = Math.max(0, (anchor || 0) / T * w - target);
}

function vCursor() {
  if (!vLive()) return;
  const { total } = vTimeline(vs.p), cur = $("#vcursor", vs.page);
  if (cur) cur.style.left = (total ? 100 * vs.t / total : 0) + "%";
  $("#vclock", vs.page).textContent = `${vTime(vs.t)} / ${vTime(total)}`;
  const sc = $("#vscroll", vs.page);
  if (sc && vs.zoom > 1 && total) {
    const x = vs.t / total * $("#vtl", vs.page).getBoundingClientRect().width;
    if (x < sc.scrollLeft + 20 || x > sc.scrollLeft + sc.clientWidth - 20) sc.scrollLeft = Math.max(0, x - sc.clientWidth / 2);
  }
}

function vDrawClips() {
  const list = $("#vclips", vs.page);
  list.innerHTML = vs.p.clips.length ? vs.p.clips.map((c, i) => {
    const it = vs.items[c.item] || {};
    return `<li class="vclip${vs.sel.k === "clip" && vs.sel.i === i ? " sel" : ""}" draggable="true" data-i="${i}" style="border-color:${vColor(c.item)}">
      <img src="${thumbUrl(c.item)}" alt="" draggable="false"><span class="vk">${i + 1} · ${c.kind === "video" ? "▶" : "📷"} ${vTime(vClipLen(c))}</span>
      <small>${esc(it.name || "")}</small></li>`;
  }).join("") : `<li class="muted" style="list-style:none">Noch leer – 🎬+ an einem Foto oder Video, oder markieren und „🎬 Video“.</li>`;
  $$(".vclip", list).forEach(li => {
    const i = +li.dataset.i;
    li.onclick = () => { vs.sel = { k: "clip", i }; vSetRate(0); vSeek(vTimeline(vs.p).tl[i].start + 0.01); vDrawTimeline(); vDrawClips(); vInspector(); };
    li.addEventListener("dragstart", e => { e.dataTransfer.setData("text/x-vclip", String(i)); e.dataTransfer.effectAllowed = "move"; });
    li.addEventListener("dragover", e => { if ([...e.dataTransfer.types].includes("text/x-vclip")) { e.preventDefault(); li.classList.add("over"); } });
    li.addEventListener("dragleave", () => li.classList.remove("over"));
    li.addEventListener("drop", e => {
      e.preventDefault();
      const from = +e.dataTransfer.getData("text/x-vclip");
      if (isNaN(from) || from === i) return;
      const [c] = vs.p.clips.splice(from, 1);
      vs.p.clips.splice(i, 0, c);
      vs.sel = { k: "clip", i }; vSave(); vRedraw();
    });
  });
}

// ------------------------------------------------------------ Einstellungen ----
function vInspector() {
  const box = $("#vinspect", vs.page), s = vs.sel;
  const bindNum = (sel, fn) => $$(sel, box).forEach(inp => inp.onchange = () => { fn(inp); vSave(); vRedraw(); });
  if (s.k === "trans" && vs.p.clips[s.i]) {
    const c = vs.p.clips[s.i];
    box.innerHTML = `<h3>Übergang ${s.i} → ${s.i + 1}</h3>
      <div class="vseg3">${VTRANS.map(([k, l, ic]) => `<button data-tr="${k}" class="${c.trans === k ? "on" : ""}">${ic} ${l}</button>`).join("")}</div>
      ${c.trans !== "cut" ? `<div class="vrow"><label>Dauer</label><input type="range" min="0.2" max="3" step="0.1" value="${c.tdur}" data-k="tdur"><span>${c.tdur.toFixed(1)} s</span></div>` : ""}
      <div class="row" style="margin-top:10px"><button id="vtrprev" title="Rechnet die Stelle um den Übergang klein (ca. 1–2 s)">✨ Übergang ansehen (gerendert)</button><button id="vtrall" title="Diese Art und Dauer an jeder Schnittstelle">Auf alle Übergänge</button></div>
      <p class="muted">Tipp: Die Marke in der Zeitleiste lässt sich ziehen – breiter = längerer Übergang.</p>`;
    $$("[data-tr]", box).forEach(b => b.onclick = () => { c.trans = b.dataset.tr; vSave(); vRedraw(); });
    const rg = $("[data-k=tdur]", box);
    if (rg) rg.oninput = () => { c.tdur = +rg.value; rg.nextElementSibling.textContent = c.tdur.toFixed(1) + " s"; vSave(); vDrawTimeline(); };
    $("#vtrprev", box).onclick = () => { const st = vTimeline(vs.p).tl[s.i].start; vPreview(Math.max(0, st - 1.5), 4 + (c.tdur || 0)); };
    $("#vtrall", box).onclick = () => { vs.p.clips.forEach((x, k) => { if (k) { x.trans = c.trans; x.tdur = c.tdur; } }); vSave(); vRedraw(); toast("Für alle Übergänge übernommen"); };
    return;
  }
  if (s.k === "fadein" || s.k === "fadeout") {
    const key = s.k, v = vs.p[key] || 0;
    box.innerHTML = `<h3>Film ${key === "fadein" ? "einblenden (Anfang)" : "ausblenden (Ende)"}</h3>
      <div class="vrow"><label>Dauer</label><input type="range" min="0" max="5" step="0.1" value="${v}" data-k="f"><span>${v.toFixed(1)} s</span></div>
      <p class="muted">Bild aus/in Schwarz und Ton gleichzeitig. 0 = harter Anfang/Ende.</p>
      <button id="vfprev">✨ Ansehen (gerendert)</button>`;
    const rg = $("[data-k=f]", box);
    rg.oninput = () => { vs.p[key] = +rg.value; rg.nextElementSibling.textContent = vs.p[key].toFixed(1) + " s"; vSave(); vDrawTimeline(); };
    $("#vfprev", box).onclick = () => { const T = vTimeline(vs.p).total; vPreview(key === "fadein" ? 0 : Math.max(0, T - 6), 6); };
    return;
  }
  if (s.k === "music" && vs.p.music[s.i]) {
    const m = vs.p.music[s.i], T = vTimeline(vs.p).total, dur = vMusicDur(m);
    box.innerHTML = `<h3>♪ ${esc(m.name)}</h3>
      <div class="vrow"><label>Einsatz</label><input type="text" class="vtc" data-k="start" value="${vTime(m.start)}"> im Film <button id="vmhere" title="an den Abspielkopf">⇤ hier</button></div>
      <div class="vrow"><label>Liedstart</label><input type="text" class="vtc" data-k="offset" value="${vTime(m.offset)}">${dur ? ` von ${vTime(dur)}` : ""}</div>
      <div class="vrow"><label>Lautstärke</label><input type="range" min="0" max="1.5" step="0.05" value="${m.vol}" data-k="vol"><span>${Math.round(m.vol * 100)} %</span></div>
      <div class="vrow"><label>Einblenden</label><input type="range" min="0" max="10" step="0.5" value="${m.fin}" data-k="fin"><span>${m.fin.toFixed(1)} s</span></div>
      <div class="vrow"><label>Ausblenden</label><input type="range" min="0" max="10" step="0.5" value="${m.fout}" data-k="fout"><span>${m.fout.toFixed(1)} s</span></div>
      <p class="muted">Läuft ${vTime(vMusicLen(m, T || 1))} im Film. Originalton der Videos leiser: in der Spur „Originalton“ den Balken nach unten ziehen.</p>
      <div class="row"><button class="danger" id="vmdel">Musik entfernen</button></div>`;
    $$("input[type=range]", box).forEach(rg => rg.oninput = () => {
      m[rg.dataset.k] = +rg.value;
      rg.nextElementSibling.textContent = rg.dataset.k === "vol" ? Math.round(m.vol * 100) + " %" : (+rg.value).toFixed(1) + " s";
      vSave(); vDrawTimeline(); vSyncMusic(true);
    });
    bindNum(".vtc", inp => { const v = vParse(inp.value); if (v != null) m[inp.dataset.k] = Math.max(0, v); });
    $("#vmhere", box).onclick = () => { m.start = Math.round(vs.t * 10) / 10; vSave(); vRedraw(); };
    $("#vmdel", box).onclick = () => vRemoveMusic(s.i);
    return;
  }
  const c = vs.p.clips[s.i];
  if (!c) { box.innerHTML = `<p class="muted">Clip, Übergang (◐ in der Bildspur), Originalton oder Musik in der Zeitleiste anklicken.</p>`; return; }
  const it = vs.items[c.item] || {}, tr = s.i > 0 ? VTRANS.find(x => x[0] === c.trans) : null;
  box.innerHTML = `<h3>Clip ${s.i + 1} <small class="muted">${esc(it.name || "")}</small></h3>
    ${c.kind === "video" ? `
      <div class="vrow"><label>Einstieg</label><input type="text" class="vtc" data-k="in" value="${vTime(c.in)}"><button data-mark="i" title="aktuelle Stelle (I)">I hier</button></div>
      <div class="vrow"><label>Ausstieg</label><input type="text" class="vtc" data-k="out" value="${vTime(c.out)}"><button data-mark="o" title="aktuelle Stelle (O)">O hier</button></div>
      <div class="vrow muted"><label></label>Länge ${vTime(c.out - c.in)}${it.duration ? ` von ${vTime(it.duration)} <button id="vfull">ganzes Video</button>` : ""}</div>
      <div class="vrow"><label>Originalton</label><input type="range" data-k="vol" min="0" max="1.5" step="0.05" value="${c.vol}"><span>${Math.round(c.vol * 100)} %</span></div>`
    : `<div class="vrow"><label>Dauer</label><input type="text" class="vtc" data-k="dur" value="${vTime(c.dur)}"> <button data-mark="o" title="Ende an den Abspielkopf (O)">O hier</button></div>
      <div class="vrow"><label></label><label><input type="checkbox" data-k="kb"${c.kb ? " checked" : ""}> Kamerafahrt (langsamer Zoom)</label></div>`}
    ${tr ? `<div class="vrow"><label>Übergang</label><a href="#" id="vtrlink">${tr[2]} ${tr[1]}${c.trans !== "cut" ? " " + c.tdur.toFixed(1) + " s" : ""} – ändern</a></div>` : ""}
    <div class="row" style="margin-top:12px"><button data-act="left" title="nach vorn">◀</button><button data-act="right" title="nach hinten">▶</button>
      <button data-act="dup">Duplizieren</button><button data-act="split">✂ Teilen</button><button data-act="del" class="danger">Entfernen</button></div>`;
  bindNum(".vtc", inp => {
    const v = vParse(inp.value);
    if (v == null) return;
    if (inp.dataset.k === "in") c.in = Math.max(0, Math.min(v, c.out - 0.2));
    else if (inp.dataset.k === "out") c.out = Math.max(c.in + 0.2, Math.min(v, it.duration || v));
    else c.dur = Math.max(0.5, Math.min(120, v));
  });
  const vol = $("[data-k=vol]", box);
  if (vol) vol.oninput = () => { c.vol = +vol.value; vol.nextElementSibling.textContent = Math.round(c.vol * 100) + " %"; vSave(); vDrawTimeline(); vShowAt(vs.t, true); };
  const kb = $("[data-k=kb]", box);
  if (kb) kb.onchange = () => { c.kb = kb.checked; vSave(); };
  const full = $("#vfull", box);
  if (full) full.onclick = () => { c.in = 0; c.out = it.duration; vSave(); vRedraw(); };
  const tl_ = $("#vtrlink", box);
  if (tl_) tl_.onclick = e => { e.preventDefault(); vs.sel = { k: "trans", i: s.i }; vDrawTimeline(); vInspector(); };
  $$("[data-mark]", box).forEach(b => b.onclick = () => vMark(b.dataset.mark));
  $$("[data-act]", box).forEach(b => b.onclick = () => {
    const a = b.dataset.act, i = s.i;
    if (a === "del") return vRemove(i);
    if (a === "split") return vSplit();
    if (a === "dup") vs.p.clips.splice(i + 1, 0, Object.assign({}, c, { id: Math.random().toString(36).slice(2, 12) }));
    if (a === "left" && i > 0) { [vs.p.clips[i - 1], vs.p.clips[i]] = [vs.p.clips[i], vs.p.clips[i - 1]]; vs.sel.i--; }
    if (a === "right" && i < vs.p.clips.length - 1) { [vs.p.clips[i + 1], vs.p.clips[i]] = [vs.p.clips[i], vs.p.clips[i + 1]]; vs.sel.i++; }
    vSave(); vRedraw();
  });
}

// Clip unter dem Abspielkopf
function vClipAt(t) {
  const { tl } = vTimeline(vs.p);
  for (let i = tl.length - 1; i >= 0; i--) if (t >= tl[i].start && t < tl[i].start + tl[i].len) return i;
  return tl.length ? tl.length - 1 : -1;
}
// I/O: Ein-/Ausstieg des Clips unter dem Abspielkopf an diese Stelle
function vMark(which) {
  const i = vClipAt(vs.t), c = vs.p.clips[i];
  if (!c) return;
  const { tl } = vTimeline(vs.p), lt = vs.t - tl[i].start;
  if (c.kind === "video") {
    const src = c.in + lt;
    if (which === "i" && src < c.out - 0.2) c.in = Math.round(src * 100) / 100;
    else if (which === "o" && src > c.in + 0.2) c.out = Math.round(src * 100) / 100;
    else return toast("So nicht möglich – Ausstieg muss nach dem Einstieg liegen");
  } else {
    if (which === "o" && lt > 0.5) c.dur = Math.round(lt * 10) / 10;
    else if (which === "i" && c.dur - lt > 0.5) c.dur = Math.round((c.dur - lt) * 10) / 10;
    else return;
  }
  vs.sel = { k: "clip", i };
  vSave();
  vRedraw();
  if (which === "i") vSeek(vTimeline(vs.p).tl[i].start);
}

function vRemove(i) {
  vs.p.clips.splice(i, 1);
  vs.sel = { k: "clip", i: Math.max(0, Math.min(i, vs.p.clips.length - 1)) };
  vSave(); vRedraw();
}

// Rasierklinge: aus einem Clip werden an dieser Stelle zwei
function vSplit() {
  const i = vClipAt(vs.t), c = vs.p.clips[i];
  if (!c) return;
  const lt = vs.t - vTimeline(vs.p).tl[i].start;
  if (lt < 0.3 || vClipLen(c) - lt < 0.3) return toast("Zu nah am Clip-Rand");
  const b = Object.assign({}, c, { id: Math.random().toString(36).slice(2, 12), trans: "cut" });
  if (c.kind === "video") { b.in = c.in + lt; c.out = b.in; } else { b.dur = c.dur - lt; c.dur = lt; }
  vs.p.clips.splice(i + 1, 0, b);
  vs.sel = { k: "clip", i: i + 1 };
  vSave(); vRedraw();
}

// ------------------------------------------------------------ Musik ----
function vMusicMenu(btn) {
  const r = btn.getBoundingClientRect();
  const lib = vs.library;
  const p = popover(r.left, r.bottom + 6, `<div class="menu vmmenu"><b style="display:block;padding:6px 10px">Musik an den Abspielkopf (${vTime(vs.t)})</b>
    ${lib.map((m, i) => `<div data-l="${i}">♪ ${esc(m.name)} <small class="muted">${m.duration ? vTime(m.duration) : ""}</small></div>`).join("")
      || '<div class="muted" style="cursor:default">Ordner FotoArchiv\\Musik ist leer</div>'}
    <hr><div data-file>📁 Audiodatei vom Computer wählen …</div></div>`);
  $$("[data-l]", p).forEach(d => d.onclick = () => { closePopover(); vAddMusic(lib[+d.dataset.l]); });
  $("[data-file]", p).onclick = async () => {
    closePopover();
    const path = await browseAudio();
    if (!path) return;
    try {
      const r2 = await api("/api/music/import", { path });
      const m = { path: r2.path, name: r2.name, duration: r2.duration };
      if (!vs.library.some(x => x.path === m.path)) vs.library.push(m);
      vAddMusic(m);
      toast("Übernommen in FotoArchiv\\Musik");
    } catch (e) { toast(e.message, 6000); }
  };
}
function vAddMusic(m) {
  vs.p.music.push({ path: m.path, name: m.name, start: Math.round(vs.t * 10) / 10, offset: 0, vol: 0.8, fin: 1, fout: 3 });
  vs.sel = { k: "music", i: vs.p.music.length - 1 };
  vSave(); vRedraw(); vSyncMusic(true);
}
function vRemoveMusic(i) {
  vs.p.music.splice(i, 1);
  vs.sel = { k: "clip", i: 0 };
  vSave(); vRedraw(); vSyncMusic(true);
}
// Dateiauswahl nur mit Audiodateien (Server listet Laufwerke, Ordner, MP3/M4A/WAV …)
function browseAudio() {
  return new Promise(res => {
    const p = modal(`<div class="fsb"><b>Audiodatei wählen</b><div class="row roots"></div><div class="cur"></div><div class="list"></div>
      <div class="row"><button id="fsno">Abbrechen</button></div></div>`);
    const load = async path => {
      const d = await api("/api/fs?audio=1&path=" + encodeURIComponent(path || ""));
      $(".roots", p).innerHTML = d.roots.map(r => `<button class="ghost" data-p="${esc(r.path)}">${esc(r.name)}</button>`).join("");
      $(".cur", p).textContent = d.path || "Laufwerk oder Ordner wählen";
      $(".list", p).innerHTML = (d.parent ? `<div data-p="${esc(d.parent)}">⬆ eine Ebene höher</div>` : "") +
        d.entries.map(e => `<div data-p="${esc(e.path)}" data-t="${e.type}">${e.type === "audio" ? "♪" : "📁"} ${esc(e.name)}${e.size ? ` <small class="muted">${fmtSize(e.size)}</small>` : ""}</div>`).join("");
      $$("[data-p]", p).forEach(x => x.onclick = () => { if (x.dataset.t === "audio") { closePopover(); res(x.dataset.p); } else load(x.dataset.p); });
    };
    $("#fsno", p).onclick = () => { closePopover(); res(null); };
    load("").catch(() => {});
  });
}

function vSyncMusic(force) {
  // je Spur ein <audio>; Position = Liedstart + (Sequenzzeit − Einsatz). Nur bei normaler Geschwindigkeit.
  if (!vs.music) return;
  while (vs.music.length > vs.p.music.length) vs.music.pop().pause();
  vs.p.music.forEach((m, i) => {
    let a = vs.music[i];
    const src = "/music?f=" + encodeURIComponent(m.path);
    if (!a) { a = vs.music[i] = new Audio(); a.preload = "auto"; }
    if (a.dataset.src !== src) { a.src = src; a.dataset.src = src; }
    const pos = m.offset + vs.t - m.start, ln = vMusicLen(m, vTimeline(vs.p).total || 1), lt = vs.t - m.start;
    const inside = lt >= 0 && lt < ln;
    // Lautstärke mit Ein-/Ausblenden wie im fertigen Film
    let g = 1;
    if (m.fin > 0) g = Math.min(g, lt / m.fin);
    if (m.fout > 0) g = Math.min(g, (ln - lt) / m.fout);
    a.volume = Math.max(0, Math.min(1, m.vol * Math.max(0, g)));
    if (vs.rate !== 1 || !inside) { if (!a.paused) a.pause(); return; }
    if (force || Math.abs(a.currentTime - pos) > 0.3) try { a.currentTime = pos; } catch (e) {}
    if (a.paused) a.play().catch(() => {});
  });
}

// ------------------------------------------------------------ Player ----
// Sequenzzeit 0…Gesamtdauer; Videos laufen in einem von zwei <video> (A/B), das nächste lädt vor.
// Die Live-Vorschau zeigt Übergänge als harten Schnitt – „Vorschau ab hier“ rechnet sie richtig.
const vSrcCache = {};
async function vVideoSrc(id) {
  const it = vs.items[id];
  if (!it) return null;
  if (VDIRECT.includes(it.ext)) return "/original/" + id;
  if (vSrcCache[id]) return vSrcCache[id];
  let s = await api(`/api/video/${id}/prepare`, {}).catch(() => null);
  while (s && s.state !== "done" && s.state !== "error" && vLive()) {
    const w = $(".vwait", vs.page);
    w.innerHTML = `<div class="vspin"></div><b>Video wird für den Browser vorbereitet …</b> ${s.progress ? Math.round(s.progress * 100) + " %" : ""}`;
    w.hidden = false;
    await new Promise(r => setTimeout(r, 1000));
    s = await api(`/api/video/${id}/status`).catch(() => null);
  }
  if (vLive()) $(".vwait", vs.page).hidden = true;
  return s && s.state === "done" ? (vSrcCache[id] = "/video/" + id) : null;
}

async function vShowAt(t, keepPlaying) {
  if (!vLive()) return;
  const { tl, total } = vTimeline(vs.p);
  vs.t = Math.max(0, Math.min(t, total));
  vCursor();
  const i = vClipAt(vs.t), c = vs.p.clips[i];
  if (!c) { vs.img.hidden = true; vs.A.hidden = vs.B.hidden = true; return; }
  if (c.kind === "photo") {
    [vs.A, vs.B].forEach(v => { v.pause(); v.hidden = true; });
    const src = `/preview/${c.item}` + (thumbVer[c.item] ? `?v=${thumbVer[c.item]}` : "");
    if (vs.img.getAttribute("src") !== src) vs.img.src = src;
    vs.img.hidden = false;
  } else {
    const src = await vVideoSrc(c.item);
    if (!src || !vLive() || vClipAt(vs.t) !== i) return;
    vs.img.hidden = true;
    let v = [vs.A, vs.B].find(x => x.dataset.src === src);
    if (!v) { v = vs.A.hidden ? vs.A : vs.B; v.src = src; v.dataset.src = src; }
    const other = v === vs.A ? vs.B : vs.A;
    other.pause(); other.hidden = true;
    v.hidden = false;
    v.volume = Math.min(1, c.vol);
    v.playbackRate = vs.rate > 0 ? vs.rate : 1;
    v.muted = vs.rate !== 1;
    v.style.transform = vs.items[c.item] && vs.items[c.item].userrot ? `rotate(${vs.items[c.item].userrot}deg)` : "";
    const want = c.in + (vs.t - tl[i].start);
    if (Math.abs((v.currentTime || 0) - want) > 0.15) try { v.currentTime = want; } catch (e) {}
    if (vs.rate > 0 && keepPlaying !== false) v.play().catch(() => {}); else v.pause();
    const n = vs.p.clips[i + 1];  // nächstes Video schon laden
    if (n && n.kind === "video" && VDIRECT.includes((vs.items[n.item] || {}).ext)) {
      const nsrc = "/original/" + n.item;
      if (other.dataset.src !== nsrc) { other.src = nsrc; other.dataset.src = nsrc; other.currentTime = n.in; }
    }
  }
  vSyncMusic();
}
function vSeek(t) { vShowAt(t, true); }

// Tempo: 0 = steht, 1 = normal, 2/4/8 = schneller vorwärts, −1/−2/−4/−8 = rückwärts (bildweise)
function vSetRate(r, quiet) {
  vs.rate = r;
  cancelAnimationFrame(vs.raf);
  if (quiet || !vLive()) return;
  $("#vplay", vs.page).textContent = r === 0 ? "▶" : "⏸";
  $("#vrate", vs.page).textContent = r === 0 ? "—" : r + "×";
  if (r === 0) { [vs.A, vs.B].forEach(v => v.pause()); vSyncMusic(); return; }
  const total = vTimeline(vs.p).total;
  if (r > 0 && vs.t >= total - 0.05) vs.t = 0;
  let last = performance.now(), lastSeek = 0;
  vShowAt(vs.t, true);
  const tick = now => {
    if (vs.rate === 0 || !vLive()) return;
    const { tl, total } = vTimeline(vs.p);
    const dt = (now - last) / 1000;
    last = now;
    const i = vClipAt(vs.t), c = vs.p.clips[i];
    const v = [vs.A, vs.B].find(x => !x.hidden);
    if (vs.rate > 0 && c && c.kind === "video" && v && !v.paused && v.readyState >= 2) vs.t = tl[i].start + (v.currentTime - c.in);
    else vs.t += dt * vs.rate;
    if (vs.t >= total) { vs.t = total; vSetRate(0); vShowAt(total, false); return; }
    if (vs.t <= 0) { vs.t = 0; vSetRate(0); vShowAt(0, false); return; }
    const ni = vClipAt(vs.t);
    if (vs.rate < 0) {
      if (now - lastSeek > 90) { lastSeek = now; vShowAt(vs.t, false); } else vCursor();
    } else if (ni !== i || (c && c.kind === "video" && v && v.currentTime >= c.out)) {
      vShowAt(ni !== i ? Math.max(vs.t, tl[ni].start) : tl[i].start + tl[i].len, true);
    } else vCursor();
    if (now - (vs.lastSync || 0) > 500) { vs.lastSync = now; vSyncMusic(); }
    vs.raf = requestAnimationFrame(tick);
  };
  vs.raf = requestAnimationFrame(tick);
}

// Vorschau: kurzen Ausschnitt mit Übergängen und gemischtem Ton auf dem Server rechnen und abspielen
async function vPreview(t0, seconds) {
  if (!vs.p.clips.length) return;
  vSetRate(0);
  const btn = $("#vprevbtn", vs.page), wait = $(".vwait", vs.page);
  const T = vTimeline(vs.p).total, a = Math.max(0, Math.min(t0, T)), b = Math.min(T, a + seconds);
  btn.disabled = true;
  wait.innerHTML = `<div class="vspin"></div><b>Effekt-Vorschau wird gerechnet …</b><br>${vTime(a)} – ${vTime(b)} (${Math.round(b - a)} s), klein, mit Übergängen und Ton`;
  wait.hidden = false;
  try {
    const r = await api(`/api/vprojects/${vs.pid}/preview`, { data: vs.p, t: t0, seconds });
    if (!vLive()) return;
    const box = $(".vprev", vs.page), v = $("video", box);
    vs.prev = { t0: r.t0, t1: Math.min(T, r.t0 + seconds) };
    $("#vprevrange", vs.page).textContent = `${vTime(vs.prev.t0)} – ${vTime(vs.prev.t1)}`;
    v.src = r.url;
    box.hidden = false;
    v.play().catch(() => {});
    vs.t = r.t0;
    vDrawTimeline();
    v.ontimeupdate = () => { vs.t = vs.prev ? vs.prev.t0 + v.currentTime : vs.t; vCursor(); };
  } catch (e) { toast(e.message, 6000); }
  finally { if (vLive()) { btn.disabled = false; wait.hidden = true; } }
}
function vClosePreview() {
  const box = $(".vprev", vs.page), v = $("video", box);
  v.pause();
  v.removeAttribute("src");
  v.load();
  box.hidden = true;
  vs.prev = null;
  vDrawTimeline();
  vShowAt(vs.t, false);
}
