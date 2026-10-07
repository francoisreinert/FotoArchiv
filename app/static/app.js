"use strict";
// FotoArchiv – Oberfläche (ohne externe Bibliotheken, außer Leaflet für die Karte)

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"];
const main = $("#main");
const thumbVer = {};
const thumbUrl = id => `/thumb/${id}` + (thumbVer[id] ? `?v=${thumbVer[id]}` : "");

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.error || r.statusText), { data, status: r.status });
  return data;
}

// Fehler beim Speichern nicht still verschlucken
window.addEventListener("unhandledrejection", e => {
  const m = e.reason && e.reason.message || String(e.reason);
  toast("Fehler: " + (m === "database is locked" ? "Katalog gerade beschäftigt, bitte gleich nochmal versuchen" : m), 5000);
});

function toast(msg, ms = 2500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), ms);
}

function fmtDate(taken, withTime = true) {
  if (!taken) return "ohne Datum";
  const [d, t] = taken.split(" ");
  const [y, m, day] = d.split("-");
  return `${+day}. ${MONTHS[+m - 1]} ${y}` + (withTime && t && t !== "00:00:00" ? `, ${t.slice(0, 5)} Uhr` : "");
}
function groupLabel(key) {
  if (key === "ohne") return "Ohne Datum";
  if (key === "ordner") return "";
  const [y, m] = key.split("-");
  return `${MONTHS[+m - 1]} ${y}`;
}
function fmtSize(b) {
  if (!b) return "";
  return b > 1e9 ? (b / 1e9).toFixed(1) + " GB" : b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.round(b / 1e3) + " KB";
}

// ------------------------------------------------------------- Zustand ----
const state = {
  persons: [],          // Liste aller Personen
  chips: [],            // gewählte Personen-Filter (ids)
  filters: { q: "", from: "", to: "", kind: "", fav: false, nodate: false, dups: false, sort: "desc" },
  zoom: +(localStorageGet("zoom") || 190),
  priv: { password: false, unlocked: null },  // null = noch unbekannt (erster Statusabruf)
};
function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* egal */ } }

async function loadPersons() {
  state.persons = await api("/api/persons");
  return state.persons;
}
const personName = id => (state.persons.find(p => p.id === +id) || {}).name || "?";

function filterParams(extra = {}) {
  const f = state.filters;
  const p = new URLSearchParams();
  if (f.q) p.set("q", f.q);
  if (state.chips.length) p.set("persons", state.chips.join(","));
  if (f.from) p.set("from", f.from);
  if (f.to) p.set("to", f.to);
  if (f.kind) p.set("kind", f.kind);
  if (f.fav) p.set("fav", "1");
  if (f.nodate) p.set("nodate", "1");
  if (f.dups) p.set("dups", "1");
  if (f.sort !== "desc") p.set("sort", f.sort);
  for (const [k, v] of Object.entries(extra)) if (v !== undefined && v !== null) p.set(k, v);
  return p;
}
function hasFilters() {
  const f = state.filters;
  return !!(f.q || state.chips.length || f.from || f.to || f.kind || f.fav || f.nodate);
}

// ------------------------------------------------- Virtuelles Raster ----
class VGrid {
  constructor(container, result, opts = {}) {
    this.c = container;
    this.ids = result.ids;
    this.groups = result.groups;
    this.videos = new Set(result.videos);
    this.lockedSet = new Set(result.locked || []);
    window.lockedIds = new Set((result.locked || []).map(i => result.ids[i]));
    this.events = result.events || {};
    this.opts = opts;
    this.context = opts.context || {};
    this.sel = new Set();
    this.anchor = null;
    this.el = document.createElement("div");
    this.el.className = "vgrid";
    container.appendChild(this.el);
    this.rendered = new Map();
    this.onScroll = () => this.render();
    main.addEventListener("scroll", this.onScroll, { passive: true });
    this.ro = new ResizeObserver(() => { if (this.el.clientWidth !== this.lastW) this.layout(); });
    this.ro.observe(this.el);
    this.el.addEventListener("click", e => {
      if (e.target.closest("a")) return;
      const hchk = e.target.closest(".hchk");
      if (hchk) { this.toggleRange(+hchk.dataset.s, +hchk.dataset.e); return; }
      const vadd = e.target.closest(".vadd");
      if (vadd) { quickAddVideo([this.ids[+vadd.closest(".cell").dataset.i]], e.shiftKey); return; }
      const cell = e.target.closest(".cell");
      if (!cell) return;
      const i = +cell.dataset.i;
      if (e.shiftKey && this.anchor !== null) {
        const [a, b] = [Math.min(this.anchor, i), Math.max(this.anchor, i)];
        for (let k = a; k <= b; k++) this.sel.add(k);
        this.selChanged();
      } else if (this.lockedSet.has(i) && !this.sel.size && !e.target.closest(".chk")) {
        unlockDialog().then(ok => ok && route());
      } else if (e.target.closest(".chk") || e.ctrlKey || e.metaKey || this.sel.size) {
        this.sel.has(i) ? this.sel.delete(i) : this.sel.add(i);
        this.anchor = i;
        this.selChanged();
      } else {
        openViewer(this.ids, i);
      }
    });
    this.layout();
  }
  destroy() {
    main.removeEventListener("scroll", this.onScroll);
    this.ro.disconnect();
    this.sel.clear();
    updateSelbar(null);
  }
  toggleRange(a, b) {
    let all = true;
    for (let k = a; k < b; k++) if (!this.sel.has(k)) { all = false; break; }
    for (let k = a; k < b; k++) all ? this.sel.delete(k) : this.sel.add(k);
    this.selChanged();
  }
  selectAll() { for (let k = 0; k < this.ids.length; k++) this.sel.add(k); this.selChanged(); }
  clearSel() { this.sel.clear(); this.anchor = null; this.selChanged(); }
  selectedIds() { return [...this.sel].sort((a, b) => a - b).map(i => this.ids[i]); }
  selChanged() {
    this.el.classList.toggle("selecting", this.sel.size > 0);
    $$(".cell", this.el).forEach(c => c.classList.toggle("sel", this.sel.has(+c.dataset.i)));
    updateSelbar(this);
  }
  layout() {
    const w = this.el.clientWidth;
    if (!w) return;
    this.lastW = w;
    // Was oben sichtbar war, merken: neue Breite = neue Zeilenpositionen (z. B. wenn die Bildlaufleiste erscheint)
    const anchor = this.rows ? this.topAnchor() : null;
    const cols = Math.max(2, Math.round(w / state.zoom));
    const size = w / cols;
    const H = this.groups.length > 1 || (this.groups[0] && this.groups[0][0] !== "ordner") ? 46 : 0;
    this.rows = [];
    this.groupTop = {};
    let y = 0, idx = 0;
    for (const [key, count] of this.groups) {
      this.groupTop[key] = y;
      if (H) { this.rows.push({ t: "h", y, h: H, key, count, start: idx }); y += H; }
      for (let i = 0; i < count; i += cols) {
        this.rows.push({ t: "r", y, h: size, start: idx + i, end: idx + Math.min(count, i + cols) });
        y += size;
      }
      idx += count;
    }
    this.cols = cols; this.size = size;
    this.el.style.height = y + "px";
    this.el.innerHTML = "";
    this.rendered.clear();
    if (anchor) this.restoreAnchor(anchor);
    this.render();
  }
  topAnchor() {
    // erste Zeile (Monatskopf oder Bildzeile), die oben im Fenster sichtbar ist
    const st = main.scrollTop - this.top();
    if (st <= 0) return null;
    const row = this.rows.find(r => r.y + r.h > st);
    return row ? { t: row.t, start: row.start, off: st - row.y, frac: (st - row.y) / row.h } : null;
  }
  restoreAnchor(a) {
    const row = this.rows.find(r => r.t === a.t && (a.t === "h" ? r.start === a.start : a.start >= r.start && a.start < r.end));
    if (row) main.scrollTop = row.y + this.top() + (a.t === "h" ? a.off : a.frac * row.h);
  }
  top() { return this.el.offsetTop; }
  render() {
    if (!this.rows) return;
    const st = main.scrollTop - this.top(), vh = main.clientHeight;
    const lo = st - vh, hi = st + 2 * vh;
    // binäre Suche nach erster sichtbarer Zeile
    let a = 0, b = this.rows.length - 1;
    while (a < b) { const m = (a + b) >> 1; if (this.rows[m].y + this.rows[m].h < lo) a = m + 1; else b = m; }
    const want = new Set();
    for (let r = a; r < this.rows.length && this.rows[r].y < hi; r++) want.add(r);
    for (const [r, els] of this.rendered) if (!want.has(r)) { els.forEach(e => e.remove()); this.rendered.delete(r); }
    const frag = document.createDocumentFragment();
    for (const r of want) {
      if (this.rendered.has(r)) continue;
      const row = this.rows[r], els = [];
      if (row.t === "h") {
        const h = document.createElement("div");
        h.className = "h";
        h.style.cssText = `top:${row.y}px;height:${row.h}px`;
        const evs = this.events[row.key] || [];
        h.innerHTML = `${esc(groupLabel(row.key))}<small>${row.count}</small>` +
          (evs.length ? `<span class="evs">${evs.map(e => `<a href="#/ereignis/${e.id}">${esc(e.name)}</a>`).join(" · ")}</span>` : "") +
          `<span class="hchk" data-s="${row.start}" data-e="${row.start + row.count}">Monat auswählen</span>`;
        els.push(h);
      } else {
        for (let i = row.start; i < row.end; i++) {
          const d = document.createElement("div");
          d.className = "cell" + (this.sel.has(i) ? " sel" : "");
          d.dataset.i = i;
          d.style.cssText = `top:${row.y}px;left:${(i - row.start) * this.size}px;width:${this.size}px;height:${this.size}px`;
          d.innerHTML = this.lockedSet.has(i) ? `<div class="lockcell" title="Privat – zum Entsperren klicken">🔒</div><span class="chk"></span>`
            : `<img decoding="async" src="${thumbUrl(this.ids[i])}" alt=""><span class="chk"></span>` + (this.videos.has(i) ? '<span class="vid">▶</span>' : "")
              + `<span class="vadd" title="Zum Videoprojekt hinzufügen (Shift+Klick: anderes Projekt)">🎬+</span>`;
          els.push(d);
        }
      }
      els.forEach(e => frag.appendChild(e));
      this.rendered.set(r, els);
    }
    this.el.appendChild(frag);
    if (this.opts.onVisible) {
      const row = this.rows[Math.min(a + 2, this.rows.length - 1)];
      this.opts.onVisible(row);
    }
  }
  scrollToGroupPrefix(prefix) {
    const key = Object.keys(this.groupTop).find(k => k.startsWith(prefix));
    if (key !== undefined) main.scrollTop = this.groupTop[key] + this.top() - 4;
  }
  scrollToIndex(i) {
    const row = this.rows.find(r => r.t === "r" && i >= r.start && i < r.end);
    if (row && (row.y + this.top() < main.scrollTop || row.y + this.top() + row.h > main.scrollTop + main.clientHeight))
      main.scrollTop = row.y + this.top() - main.clientHeight / 3;
  }
}

let currentGrid = null;
function clearMain() {
  if (currentGrid) { currentGrid.destroy(); currentGrid = null; }
  if (window._map) { window._map.remove(); window._map = null; }
  main.innerHTML = "";
  main.scrollTop = 0;
}

function zoomButtons() {
  return `<div class="zoom"><button data-z="120" title="Klein">S</button><button data-z="190" title="Mittel">M</button><button data-z="300" title="Groß">L</button></div>`;
}
function bindZoom(el) {
  el.querySelectorAll("[data-z]").forEach(b => b.onclick = () => {
    state.zoom = +b.dataset.z;
    localStorageSet("zoom", state.zoom);
    if (currentGrid) currentGrid.layout();
  });
}

async function showGrid(title, extra = {}, opts = {}) {
  clearMain();
  const head = document.createElement("div");
  head.className = "gridhead";
  head.innerHTML = `<h2 style="margin:0;font-size:20px">${esc(title)}</h2><span class="count">lädt …</span>${opts.headExtra || ""}
    <button class="ghost" data-show title="Diashow aller Fotos dieser Ansicht">▶ Diashow</button>${zoomButtons()}`;
  $("[data-show]", head).onclick = () => currentGrid && slideshowDialog(currentGrid.ids, title);
  main.appendChild(head);
  bindZoom(head);
  if (opts.bind) opts.bind(head);
  const res = await api("/api/query?" + filterParams(extra));
  $(".count", head).textContent = res.total.toLocaleString("de-DE") + (res.total === 1 ? " Element" : " Elemente");
  if (!res.total) {
    main.insertAdjacentHTML("beforeend", `<div class="empty">${opts.empty || "Keine Fotos gefunden."}</div>`);
    return null;
  }
  currentGrid = new VGrid(main, res, { context: opts.context });
  return currentGrid;
}

// --------------------------------------------------------------- Seiten ----
const routes = {
  async start() {
    clearMain();
    const page = document.createElement("div");
    page.className = "page";
    page.innerHTML = `<h1>Übersicht</h1><div class="empty">Statistik wird geladen …</div>`;
    main.appendChild(page);
    const s = await api("/api/stats");
    const num = n => (n || 0).toLocaleString("de-DE");
    const year = t => t ? t.slice(0, 4) : "";
    const tile = (href, n, label, note) => `<${href ? `a href="${href}"` : "div"} class="stat"><b>${num(n)}</b><span>${label}</span>${note ? `<small>${note}</small>` : ""}</${href ? "a" : "div"}>`;
    const tb = b => b > 1e12 ? (b / 1e12).toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " TB" : fmtSize(b);
    const maxY = Math.max(1, ...s.years.map(y => y.photo + y.video));
    const peak = s.years.reduce((a, y) => (y.photo + y.video > a.photo + a.video ? y : a), s.years[0] || { photo: 0, video: 0 });
    const every = s.years.length > 30 ? 5 : s.years.length > 14 ? 2 : 1;
    page.innerHTML = `<h1>Übersicht <small>${s.first ? `${year(s.first)} – ${year(s.last)} · ` : ""}${tb(s.size)}</small>
        <span style="flex:1"></span><a href="#/fotos"><button class="primary">Fotos ansehen →</button></a></h1>
      <div class="stats">
        ${tile("#/fotos", s.photo, "Fotos", s.raw ? `+ ${num(s.raw)} RAW` : "")}
        ${tile("#/fotos", s.video, "Videos", fmtSize(s.video_size))}
        ${tile("#/personen", s.persons, "Personen", `${num(s.faces)} benannte Gesichter`)}
        ${tile("#/alben", s.albums, "Alben", `${num(s.events)} Ereignisse`)}
        ${tile("#/ordner", s.folders, "Ordner")}
        ${tile("#/karte", s.geo, "mit Ort (GPS)")}
        ${tile("#/favoriten", s.fav, "Favoriten")}
        ${tile("#/duplikate", s.dups, "Duplikate", s.dups ? `${fmtSize(s.dups_size)} – prüfen und aufräumen` : "")}
        ${s.hidden ? tile("#/ausgeblendet", s.hidden, "Ausgeblendet") : ""}
        ${s.trash.count ? tile("#/papierkorb", s.trash.count, "im Papierkorb", fmtSize(s.trash.size)) : ""}
      </div>
      ${s.years.length ? `<div class="card wide"><h2>Fotos und Videos pro Jahr</h2>
        <div class="yearchart">${s.years.map(y => `<a href="#/fotos?jahr=${y.year}" data-y="${y.year}" data-p="${y.photo}" data-v="${y.video}">
          <i style="height:${Math.max(1, (y.photo + y.video) / maxY * 100)}%"></i></a>`).join("")}
          <div class="charttip" hidden></div></div>
        <div class="yearaxis">${s.years.map((y, i) => `<span>${i % every === 0 || y === peak ? y.year : ""}</span>`).join("")}</div>
        <div class="muted" style="margin-top:8px">Meiste Aufnahmen: ${peak.year} mit ${num(peak.photo + peak.video)}. Klick auf ein Jahr zeigt dessen Fotos.</div></div>` : ""}
      <div class="statcols">
        <div class="card" id="toppers" hidden><h2>Häufigste Personen</h2><div></div></div>
        ${s.places.length ? `<div class="card"><h2>Häufigste Orte</h2>${s.places.map(([pl, n]) => `<a class="toprow" href="#/fotos" data-q="${esc(pl)}">
          <span>${esc(pl)}</span><small>${num(n)}</small></a>`).join("")}</div>` : ""}
      </div>
      ${s.last_index ? `<p class="muted">Zuletzt eingelesen: ${esc(fmtDate(s.last_index.replace("T", " ")))}</p>` : ""}`;
    personsReady.then(() => {
      const persons = state.persons.filter(p => !p.hidden && p.items).sort((a, b) => b.items - a.items).slice(0, 8);
      const box = $("#toppers", page);
      if (!persons.length || !box) return;
      $("div", box).innerHTML = persons.map(p => `<a class="toprow" href="#/person/${p.id}">
        ${p.cover ? `<img src="/face/${p.cover}" alt="">` : `<span class="noface"></span>`}<span>${esc(p.name)}</span><small>${num(p.items)}</small></a>`).join("");
      box.hidden = false;
    });
    const chart = $(".yearchart", page);
    if (chart) {
      const tip = $(".charttip", chart);
      $$("a", chart).forEach(a => {
        a.onmouseenter = () => {
          tip.innerHTML = `<b>${a.dataset.y}</b><br>${num(+a.dataset.p)} Fotos · ${num(+a.dataset.v)} Videos`;
          tip.hidden = false;
          const r = a.getBoundingClientRect(), c = chart.getBoundingClientRect();
          tip.style.left = Math.min(Math.max(r.left - c.left + r.width / 2, 70), c.width - 70) + "px";
          tip.style.top = (r.bottom - c.top - $("i", a).offsetHeight - 6) + "px";
        };
        a.onmouseleave = () => (tip.hidden = true);
      });
    }
    $$("[data-q]", page).forEach(a => a.onclick = e => {
      e.preventDefault();
      search.value = a.dataset.q;
      runSearch();
    });
  },
  async fotos() {
    const g = await showGrid(hasFilters() ? "Suchergebnis" : "Alle Fotos", {}, {
      empty: hasFilters() ? "Keine Treffer für diese Suche." : "Noch keine Fotos im Katalog. Unter <a href='#/einstellungen'>Bibliothek</a> den Index starten.",
    });
    const qs = new URLSearchParams(location.hash.split("?")[1] || "");
    const y = qs.get("zu") || qs.get("jahr");
    if (g && y) g.scrollToGroupPrefix(y);
  },
  async foto(id) {
    const g = await routes.fotos();
    const ids = currentGrid ? currentGrid.ids : [];
    const i = ids.indexOf(+id);
    openViewer(i >= 0 ? ids : [+id], Math.max(0, i));
    return g;
  },
  async kalender() {
    clearMain();
    const page = document.createElement("div");
    page.className = "page";
    page.innerHTML = `<h1>Kalender <small>lädt …</small></h1>`;
    main.appendChild(page);
    const cal = await api("/api/calendar");
    const keys = Object.keys(cal).sort();
    if (!keys.length) { page.innerHTML += `<div class="empty">Noch keine datierten Fotos.</div>`; return; }
    const y0 = +keys[0].slice(0, 4), y1 = +keys[keys.length - 1].slice(0, 4);
    const short = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"];
    let html = "";
    for (let y = y1; y >= y0; y--) {
      if (!short.some((_, m) => cal[`${y}-${String(m + 1).padStart(2, "0")}`])) continue;
      html += `<div class="yr">${y}</div>`;
      for (let m = 0; m < 12; m++) {
        const k = `${y}-${String(m + 1).padStart(2, "0")}`, c = cal[k];
        if (!c) { html += `<div class="mo empty"><span class="m">${short[m]}</span></div>`; continue; }
        const ev = c.events[0];
        html += `<a class="mo" href="${ev && c.events.length === 1 ? "#/ereignis/" + ev.id : "#/fotos?zu=" + k}" title="${esc(MONTHS[m])} ${y}: ${c.count} Fotos${c.events.length ? " – " + esc(c.events.map(e => e.name).join(", ")) : ""}">
          ${c.cover ? `<img loading="lazy" src="/thumb/${c.cover}" alt="">` : `<div class="lockcell">🔒</div>`}<span class="m">${short[m]}</span><span class="n">${c.count}</span>
          ${c.events.length ? `<span class="ev">${esc(c.events.map(e => e.name).join(" · "))}</span>` : ""}</a>`;
      }
    }
    page.innerHTML = `<h1>Kalender <small>Monat anklicken zum Ansehen, Ereignis-Monate öffnen das Ereignis</small></h1><div class="cal">${html}</div>`;
  },
  async alben(parent) {
    clearMain();
    const d = await api("/api/albums");
    const page = document.createElement("div");
    page.className = "page";
    const coverOf = (cover, priv) => cover ? `<img loading="lazy" src="/thumb/${cover}" alt="">`
      : priv && !state.priv.unlocked ? `<div class="lockcell">🔒</div>` : `<img alt="">`;
    const albumCard = a => `<a class="folder" href="#/album/${a.id}">${coverOf(a.cover, a.private_eff)}
        <div><b>${a.private_eff ? '<span class="privtag">🔒</span> ' : ""}${esc(a.name)}</b><small>${a.total.toLocaleString("de-DE")} Fotos</small></div></a>`;
    const evCard = e => `<a class="folder" href="#/ereignis/${e.id}">${coverOf(e.cover, e.private)}
        <div><b>${e.private ? '<span class="privtag">🔒</span> ' : ""}${esc(e.name)}</b><small>${fmtRange(e.start, e.end)} · ${e.count.toLocaleString("de-DE")}</small></div></a>`;
    const top = d.albums.filter(a => a.parent == null);
    page.innerHTML = `<h1>Ereignisse <small>${d.events.length}</small><span style="flex:1"></span><button id="newev">Neues Ereignis</button></h1>
      <div class="folders">${d.events.map(evCard).join("") || '<span class="muted">Keine Ereignisse.</span>'}</div>
      <h1 style="margin-top:28px">Alben <small>${d.albums.length}</small><span style="flex:1"></span><button id="newal">Neues Album</button></h1>
      <div class="folders">${top.map(albumCard).join("") || '<span class="muted">Keine Alben.</span>'}</div>
      <p class="muted" style="margin-top:24px">Tipp: In „Fotos“ Bilder markieren (Haken oben links im Bild, Shift = Bereich) und „+ Album“ oder „+ Ereignis“ wählen.
      ${d.hidden ? ` · <a href="#/ausgeblendet">Ausgeblendete Fotos (${d.hidden})</a>` : ""}</p>`;
    main.appendChild(page);
    $("#newal", page).onclick = async () => {
      const name = await askText("Name des neuen Albums", "");
      if (name) { const r = await api("/api/albums", { name }); location.hash = "#/album/" + r.id; }
    };
    $("#newev", page).onclick = async () => {
      const name = await askText("Name des Ereignisses (z. B. „Sommerurlaub 2025“)", "");
      if (!name) return;
      const r = await askRange("Zeitraum von „" + name + "“", "", "");
      if (!r) return;
      const ev = await api("/api/events", { name, ...r });
      location.hash = "#/ereignis/" + ev.id;
    };
  },
  async album(id) {
    clearMain();
    const d = await api("/api/albums");
    const a = d.albums.find(x => x.id === +id);
    if (!a) { main.innerHTML = `<div class="empty">Album nicht gefunden.</div>`; return; }
    if (a.private_eff && !state.priv.unlocked) return lockScreen(`Das Album „${a.name}“ ist privat.`);
    const kids = d.albums.filter(x => x.parent === a.id);
    const parent = d.albums.find(x => x.id === a.parent);
    const page = document.createElement("div");
    page.className = "page";
    page.style.paddingBottom = "0";
    page.innerHTML = `<div class="crumbs"><a href="#/alben">Alben</a>${parent ? ` › <a href="#/album/${parent.id}">${esc(parent.name)}</a>` : ""} › ${esc(a.name)}
        <span style="flex:1"></span><button id="apriv">${a.private ? "Privat aufheben" : "🔒 Privat"}</button>
        <button id="aren">Umbenennen</button><button id="asub">Unteralbum anlegen</button>
        <button id="amove">Verschieben</button><button id="adel" class="danger">Album löschen</button></div>
      ${a.description ? `<p class="muted">${esc(a.description)}</p>` : ""}
      ${kids.length ? `<div class="folders">${kids.map(k => `<a class="folder" href="#/album/${k.id}">
        ${k.cover ? `<img loading="lazy" src="/thumb/${k.cover}" alt="">` : `<img alt="">`}
        <div><b>${k.private_eff ? '<span class="privtag">🔒</span> ' : ""}${esc(k.name)}</b><small>${k.total.toLocaleString("de-DE")}</small></div></a>`).join("")}</div>` : ""}
      ${a.private_eff && !a.private ? `<p class="muted">🔒 Privat, weil ein übergeordnetes Album privat ist.</p>` : ""}`;
    main.appendChild(page);
    $("#apriv", page).onclick = async () => {
      if (!a.private && !await ensurePassword()) return;
      await api(`/api/albums/${a.id}`, { private: !a.private });
      toast(a.private ? "Album ist nicht mehr privat" : "Album ist jetzt privat");
      routes.album(id);
    };
    $("#aren", page).onclick = async () => {
      const name = await askText("Album umbenennen", a.name);
      if (name && name !== a.name) { await api(`/api/albums/${a.id}`, { name }); routes.album(id); }
    };
    $("#asub", page).onclick = async () => {
      const name = await askText(`Neues Unteralbum in „${a.name}“`, "");
      if (name) { const r = await api("/api/albums", { name, parent: a.id }); location.hash = "#/album/" + r.id; }
    };
    $("#amove", page).onclick = async () => {
      const r = await pickAlbum(`„${a.name}“ verschieben nach …`, { allowNew: false, allowTop: true, exclude: a.id });
      if (!r) return;
      try { await api(`/api/albums/${a.id}`, { parent: r.top ? null : r.id }); routes.album(id); }
      catch (e) { toast(e.message); }
    };
    $("#adel", page).onclick = async () => {
      const r = await confirmCheck(`Album „${a.name}“ löschen? Ohne Haken bleiben die Fotos erhalten, nur das Album verschwindet.`,
        `Auch die ${a.count.toLocaleString("de-DE")} Fotos/Videos des Albums in den Papierkorb legen`);
      if (!r) return;
      await api(`/api/albums/${a.id}/delete`, { items: r.checked });
      if (r.checked) loadYears().catch(() => {});
      location.hash = parent ? "#/album/" + parent.id : "#/alben";
    };
    const res = await api("/api/query?" + filterParams({ album: a.id }));
    const head = document.createElement("div");
    head.className = "gridhead";
    head.innerHTML = `<h2 style="margin:0;font-size:20px">${esc(a.name)}</h2><span class="count">${res.total.toLocaleString("de-DE")} Fotos</span>
      <button class="ghost" data-show>▶ Diashow</button><button class="ghost" data-share>Teilen …</button>${zoomButtons()}`;
    $("[data-show]", head).onclick = () => slideshowDialog(res.ids, a.name);
    $("[data-share]", head).onclick = () => shareDialog(res.ids, a.name);
    main.appendChild(head);
    bindZoom(head);
    if (res.total) currentGrid = new VGrid(main, res, { context: { album: a.id, albumName: a.name } });
    else main.insertAdjacentHTML("beforeend", `<div class="empty">Noch leer. In „Fotos“ Bilder markieren und „+ Album“ wählen.</div>`);
  },
  async ereignis(id) {
    const d = await api("/api/albums");
    const e = d.events.find(x => x.id === +id);
    if (!e) { clearMain(); main.innerHTML = `<div class="empty">Ereignis nicht gefunden.</div>`; return; }
    if (e.private && !state.priv.unlocked) return lockScreen(`Das Ereignis „${e.name}“ ist privat.`);
    await showGrid(e.name, { event: e.id }, {
      headExtra: `<span class="muted">${fmtRange(e.start, e.end)}</span><button id="epriv">${e.private ? "Privat aufheben" : "🔒 Privat"}</button><button id="eren">Umbenennen</button>
        <button id="edate">Zeitraum ändern</button><button id="edel" class="danger">Löschen</button>`,
      bind: head => {
        $("#epriv", head).onclick = async () => {
          if (!e.private && !await ensurePassword()) return;
          await api(`/api/events/${e.id}`, { private: !e.private });
          toast(e.private ? "Ereignis ist nicht mehr privat" : "Ereignis ist jetzt privat");
          routes.ereignis(id);
        };
        $("#eren", head).onclick = async () => {
          const name = await askText("Ereignis umbenennen", e.name);
          if (name && name !== e.name) { await api(`/api/events/${e.id}`, { name }); routes.ereignis(id); }
        };
        $("#edate", head).onclick = async () => {
          const r = await askRange("Zeitraum von „" + e.name + "“", e.start.slice(0, 10), e.end.slice(0, 10));
          if (r) { await api(`/api/events/${e.id}`, r); routes.ereignis(id); }
        };
        $("#edel", head).onclick = async () => {
          const r = await confirmCheck(`Ereignis „${e.name}“ löschen? Ohne Haken bleiben die Fotos erhalten.`,
            `Auch die ${e.count.toLocaleString("de-DE")} Fotos/Videos dieses Zeitraums in den Papierkorb legen`);
          if (!r) return;
          await api(`/api/events/${e.id}/delete`, { items: r.checked });
          if (r.checked) loadYears().catch(() => {});
          location.hash = "#/alben";
        };
      },
    });
  },
  async ausgeblendet() {
    await showGrid("Ausgeblendete Fotos", { hidden: "1" }, {
      context: { hidden: true },
      empty: "Keine ausgeblendeten Fotos. Fotos markieren und unter „Mehr …“ ausblenden – sie verschwinden dann aus allen Ansichten, bleiben aber auf der Festplatte.",
    });
  },
  async papierkorb() {
    const t = await api("/api/trash");
    await showGrid("Papierkorb", { hidden: "2" }, {
      context: { trash: true },
      headExtra: t.count ? `<span class="muted">${fmtSize(t.size)}</span><button id="trest">Alle wiederherstellen</button>
        <button id="tpurge" class="danger">Papierkorb leeren …</button>` : "",
      empty: "Der Papierkorb ist leer. Gelöschte Fotos, Videos und Ordner landen hier und bleiben auf der Platte " +
        "(Ordner „FotoArchiv-Papierkorb“), bis du den Papierkorb leerst.",
      bind: head => {
        if (!t.count) return;
        $("#trest", head).onclick = async () => {
          if (!currentGrid) return;
          const r = await api("/api/trash/restore", { ids: currentGrid.ids });
          toast(`${r.count.toLocaleString("de-DE")} wiederhergestellt`);
          reportErrors(r);
          loadYears().catch(() => {});
          route();
        };
        $("#tpurge", head).onclick = async () => {
          if (!await confirmBox(`Papierkorb leeren? ${t.count.toLocaleString("de-DE")} Dateien (${fmtSize(t.size)}) werden endgültig ` +
            "von der Platte gelöscht. Das lässt sich nicht rückgängig machen.")) return;
          toast("Papierkorb wird geleert …", 60000);
          const r = await api("/api/trash/purge", { all: true });
          toast(`${r.count.toLocaleString("de-DE")} Dateien endgültig gelöscht`);
          reportErrors(r);
          route();
        };
      },
    });
  },
  async duplikate() {
    clearMain();
    const qs = new URLSearchParams(location.hash.split("?")[1] || "");
    const off = +(qs.get("ab") || 0), kind = qs.get("art") || "", folder = qs.get("ordner") || "";
    const prefer = localStorageGet("dupPrefer") || "Bilder";
    const LIM = 20;
    const num = n => (n || 0).toLocaleString("de-DE");
    const go = (o, k = kind, f = folder) => { location.hash = "#/duplikate?" + new URLSearchParams({ ab: o, art: k, ordner: f }); };
    const page = document.createElement("div");
    page.className = "page";
    page.innerHTML = `<h1>Duplikate</h1><div class="empty">Duplikate werden zusammengestellt …</div>`;
    main.appendChild(page);
    const d = await api(`/api/dups?offset=${off}&limit=${LIM}&kind=${kind}&folder=${encodeURIComponent(folder)}&prefer=${encodeURIComponent(prefer)}`);
    const choice = {};
    d.groups.forEach(g => (choice[g.id] = g.keep));
    const opt = (v, label, cur) => `<option value="${esc(v)}"${v === cur ? " selected" : ""}>${esc(label)}</option>`;
    const pager = () => d.total > LIM ? `<div class="row dup-pager"><button data-o="${off - LIM}"${off ? "" : " disabled"}>‹ Zurück</button>
      <span class="muted">Gruppen ${num(off + 1)}–${num(Math.min(off + LIM, d.total))} von ${num(d.total)}</span>
      <button data-o="${off + LIM}"${off + LIM < d.total ? "" : " disabled"}>Weiter ›</button></div>` : "";
    const best = (g, f) => Math.max(...g.members.map(f));
    const card = g => `<div class="dupgroup" data-g="${g.id}">
      <div class="duphead"><b>${esc(fmtDate(g.members[0].taken))}</b><span class="muted">${g.members.length} gleiche ${g.members[0].kind === "video" ? "Videos" : "Fotos"} · Vorschlag: ${esc(g.reason)}</span>
        <span style="flex:1"></span><button data-act="keepall" title="z. B. Serienbilder: alle bleiben, werden nicht mehr als Duplikat angezeigt">Kein Duplikat</button>
        <button data-act="apply" class="primary">Übernehmen</button></div>
      <div class="dupmembers">${g.members.map((m, i) => `<div class="dupm" data-id="${m.id}">
        <div class="dupimg" data-i="${i}" title="Groß ansehen"><img loading="lazy" src="/thumb/${m.id}" alt="">${m.kind === "video" ? `<span class="dupplay">▶</span>` : ""}</div>
        <div class="dupbadge"></div>
        <dl>
          <dt>Ordner</dt><dd class="path">${esc(m.folder)}/<b>${esc(m.name)}</b></dd>
          <dt>Datei</dt><dd class="${m.size === best(g, x => x.size) && g.members.some(x => x.size !== m.size) ? "better" : ""}">${fmtSize(m.size)} · ${esc(m.ext.toUpperCase())}${m.has_raw ? " + RAW" : ""}</dd>
          <dt>Bild</dt><dd>${m.width ? `${m.width} × ${m.height}` : "?"}${m.duration ? ` · ${Math.round(m.duration)} s` : ""}</dd>
          <dt>Angaben</dt><dd>${[m.camera ? esc(m.camera) : "", m.lat != null ? "GPS" : "", m.albums ? `${m.albums} Album/Alben` : "",
            m.persons ? `${m.persons} Person(en)` : "", m.fav || m.rating >= 4 ? "★" : "", m.hidden ? "ausgeblendet" : ""].filter(Boolean).join(" · ") || "–"}</dd>
        </dl></div>`).join("")}</div></div>`;
    page.innerHTML = `<h1>Duplikate <small>${num(d.total)} Gruppen · ${num(d.files)} überzählige Dateien</small></h1>
      <p class="muted" style="max-width:900px">Gleiche Fotos/Videos nebeneinander. Grün = wird behalten (Vorschlag: höchste Qualität, also größte Datei bzw.
        Originalformat; bei identischen Dateien der bevorzugte Ordner). Klick auf eine andere Datei wählt sie zum Behalten, Klick aufs Bild zeigt es groß.
        „Übernehmen“ legt die roten in den Papierkorb – Alben, Personen, Favoriten und Stichwörter gehen auf die behaltene Datei über.</p>
      <div class="row dupbar">
        <label>Art <select id="dkind">${opt("", "Alle", kind)}${opt("photo", "Fotos", kind)}${opt("video", "Videos", kind)}</select></label>
        <label>Ordner <select id="dfold">${opt("", "Alle Ordner", folder)}${d.folders.map(([f, n]) => opt(f, `${f} (${num(n)})`, folder)).join("")}</select></label>
        <label>Bei gleicher Qualität behalten in <select id="dpref">${opt("", "egal", prefer)}${d.folders.map(([f]) => opt(f, f, prefer)).join("")}</select></label>
        <span style="flex:1"></span>
        ${d.groups.length ? `<button id="dpage" class="primary">Alle ${d.groups.length} Vorschläge dieser Seite übernehmen</button>
        <button id="dall" class="danger">Alle ${num(d.total)} Gruppen übernehmen …</button>` : ""}
      </div>
      <div class="card wide" id="djob" hidden></div>
      ${pager()}
      <div id="dgroups">${d.groups.map(card).join("") || `<div class="empty">Keine Duplikate${folder || kind ? " mit diesem Filter" : ""}. 🎉</div>`}</div>
      ${pager()}`;
    const paint = el => {
      const keep = choice[el.dataset.g];
      $$(".dupm", el).forEach(m => {
        const k = +m.dataset.id === keep;
        m.classList.toggle("keep", k);
        m.classList.toggle("drop", !k);
        $(".dupbadge", m).textContent = k ? "✓ Behalten" : "🗑 In den Papierkorb";
      });
    };
    const groupEl = id => $(`.dupgroup[data-g="${id}"]`, page);
    const payload = g => ({ keep: choice[g.id], remove: g.members.map(m => m.id).filter(id => id !== choice[g.id]) });
    const apply = async list => {
      if (!list.length) return;
      const r = await api("/api/dups/resolve", { groups: list.map(payload) });
      reportErrors(r);
      toast(`${num(r.count)} Duplikate in den Papierkorb gelegt`);
      return r;
    };
    $$(".dupgroup", page).forEach(el => {
      const g = d.groups.find(x => x.id === +el.dataset.g);
      paint(el);
      $$(".dupm", el).forEach(m => m.onclick = e => {
        if (e.target.closest(".dupimg")) return;
        choice[g.id] = +m.dataset.id;
        paint(el);
      });
      $$(".dupimg", el).forEach(x => x.onclick = () => openViewer(g.members.map(m => m.id), +x.dataset.i));
      $("[data-act=apply]", el).onclick = async () => { if (await apply([g])) el.remove(); };
      $("[data-act=keepall]", el).onclick = async () => {
        await api("/api/dups/keep", { ids: g.members.map(m => m.id) });
        toast("Alle behalten – wird nicht mehr als Duplikat angezeigt");
        el.remove();
      };
    });
    $$(".dup-pager [data-o]", page).forEach(b => b.onclick = () => go(Math.max(0, +b.dataset.o)));
    $("#dkind", page).onchange = e => go(0, e.target.value, folder);
    $("#dfold", page).onchange = e => go(0, kind, e.target.value);
    $("#dpref", page).onchange = e => { localStorageSet("dupPrefer", e.target.value); routes.duplikate(); };
    const dp = $("#dpage", page);
    if (dp) dp.onclick = async () => {
      const left = d.groups.filter(g => groupEl(g.id));
      const n = left.reduce((s, g) => s + g.members.length - 1, 0);
      if (!await confirmBox(`${num(n)} Dateien aus ${left.length} Gruppen in den Papierkorb legen (jeweils die rot markierten)?`)) return;
      await apply(left);
      routes.duplikate();
    };
    const job = $("#djob", page);
    const pollJob = async () => {
      const j = await api("/api/dups/job");
      if (!j.running && !j.finished) return;
      job.hidden = false;
      job.innerHTML = `<h2>${j.running ? "Duplikate werden aufgeräumt …" : "Fertig"}</h2>
        <div class="progress"><i style="width:${j.total ? (j.done / j.total * 100).toFixed(1) : 0}%"></i></div>
        <p class="muted">${num(j.done)} von ${num(j.total)} Gruppen · ${num(j.count)} Dateien im Papierkorb${j.errors.length ? ` · ${j.errors.length} Probleme, z. B. ${esc(j.errors[0])}` : ""}</p>
        ${j.running ? `<button id="dstop">Anhalten</button>` : ""}`;
      const st = $("#dstop", job);
      if (st) st.onclick = () => api("/api/dups/stop", {});
      if (j.running && location.hash.startsWith("#/duplikate")) setTimeout(pollJob, 2000);
    };
    pollJob();
    const da = $("#dall", page);
    if (da) da.onclick = async () => {
      if (!await confirmBox(`Für alle ${num(d.total)} Gruppen${folder ? ` mit Ordner „${folder}“` : ""}${kind ? (kind === "video" ? " (nur Videos)" : " (nur Fotos)") : ""} den Vorschlag übernehmen? ` +
        `${num(d.files)} Dateien wandern in den Papierkorb (bevorzugter Ordner: ${prefer || "egal"}). ` +
        "Das läuft im Hintergrund und kann bei vielen Dateien eine Weile dauern. Tipp: vorher ein paar Seiten durchsehen, und die Platte sollte gesichert sein.")) return;
      const r = await api("/api/dups/all", { prefer, kind, folder });
      if (!r.ok) return toast("Läuft bereits");
      pollJob();
    };
  },
  async favoriten() {
    await showGrid("Favoriten", { fav: "1" }, { empty: "Noch keine Favoriten. Im Betrachter mit ☆ oder Taste F markieren. Mylio-Bewertungen ab 4 Sternen erscheinen hier auch." });
  },
  async ordner(path) {
    clearMain();
    path = path || "";
    const d = await api("/api/folders?path=" + encodeURIComponent(path));
    const page = document.createElement("div");
    page.className = "page";
    const parts = path ? path.split("/") : [];
    let crumbs = `<a href="#/ordner">Alle Ordner</a>`;
    parts.forEach((p, i) => { crumbs += ` › <a href="#/ordner/${encodeURIComponent(parts.slice(0, i + 1).join("/"))}">${esc(p)}</a>`; });
    if (path) crumbs += `<span style="flex:1"></span><button id="fdel" class="danger">Ordner löschen</button>`;
    page.innerHTML = `<div class="crumbs">${crumbs}</div>` +
      (d.children.length ? `<div class="folders">${d.children.map(c => `
        <a class="folder" href="#/ordner/${encodeURIComponent(c.path)}">
          <img loading="lazy" src="/thumb/${c.cover}" alt=""><div><b>${esc(c.name)}</b><small>${c.total.toLocaleString("de-DE")}</small></div></a>`).join("")}</div>` : "") +
      (path && d.total > d.direct ? `<button class="ghost" id="allin">Alle ${d.total.toLocaleString("de-DE")} Fotos in diesem Ordner und Unterordnern zeigen</button>` : "");
    main.appendChild(page);
    const fdel = $("#fdel", page);
    if (fdel) fdel.onclick = async () => {
      if (!await confirmBox(`Ordner „${path}“ mit allen Unterordnern löschen? Die ${(d.total || 0).toLocaleString("de-DE")} Fotos/Videos darin ` +
        "(auch ausgeblendete, Duplikate und RAW) wandern in den Papierkorb und lassen sich wiederherstellen. Andere Dateien bleiben im Ordner.")) return;
      toast("Ordner wird in den Papierkorb gelegt …", 60000);
      const r = await api("/api/folders/delete", { path });
      toast(`${r.count.toLocaleString("de-DE")} Dateien in den Papierkorb gelegt` + (r.others_total
        ? ` · ${r.others_total} andere Datei(en) blieben im Ordner, z. B. ${r.others[0].split("/").pop()}` : ""), 9000);
      reportErrors(r);
      loadYears().catch(() => {});
      location.hash = "#/ordner/" + parts.slice(0, -1).map(encodeURIComponent).join("/");
    };
    const all = $("#allin", page);
    if (all) all.onclick = async () => {
      page.remove();
      await showGrid(parts[parts.length - 1], { folder: path, recursive: "1" });
    };
    if (path && d.direct) {
      const res = await api("/api/query?" + filterParams({ folder: path }));
      const head = document.createElement("div");
      head.className = "gridhead";
      head.innerHTML = `<span class="count">${res.total.toLocaleString("de-DE")} Dateien in diesem Ordner</span>${zoomButtons()}`;
      main.appendChild(head);
      bindZoom(head);
      currentGrid = new VGrid(main, res);
    } else if (!d.children.length) {
      page.insertAdjacentHTML("beforeend", `<div class="empty">Keine Ordner im Katalog.</div>`);
    }
  },
  async personen() {
    clearMain();
    const persons = await loadPersons();
    const page = document.createElement("div");
    page.className = "page";
    const visible = persons.filter(p => !p.hidden), hidden = persons.filter(p => p.hidden);
    const card = p => `<a class="person" href="#/person/${p.id}">
        ${p.cover ? `<img loading="lazy" src="/face/${p.cover}" alt="">` : `<div class="noimg"></div>`}
        <b>${esc(p.name)}${p.suggestions ? `<span class="badge" title="Vorschläge zum Bestätigen">${p.suggestions}</span>` : ""}</b>
        <small>${p.items.toLocaleString("de-DE")} Fotos</small></a>`;
    page.innerHTML = `<h1>Personen <small>${visible.length}</small></h1>
      <div class="toolbar"><a href="#/unbekannt"><button>Unbekannte Gesichter benennen</button></a>
        <button id="recomp">Erkennung aktualisieren</button>
        <span class="muted">Vorschläge (Zahl am Namen) prüfen, um die Erkennung zu verbessern.</span></div>
      <div class="persons">${visible.map(card).join("") || '<div class="empty">Noch keine Personen.</div>'}</div>
      ${hidden.length ? `<h1 style="margin-top:30px">Ausgeblendet <small>${hidden.length}</small></h1><div class="persons">${hidden.map(card).join("")}</div>` : ""}`;
    main.appendChild(page);
    $("#recomp", page).onclick = recompute;
  },
  async person(id, tab) {
    clearMain();
    await loadPersons();
    const p = state.persons.find(x => x.id === +id);
    if (!p) { main.innerHTML = `<div class="empty">Person nicht gefunden.</div>`; return; }
    tab = tab || "fotos";
    const page = document.createElement("div");
    page.className = "page";
    page.style.paddingBottom = "0";
    page.innerHTML = `<h1>${p.cover ? `<img src="/face/${p.cover}" style="width:48px;height:48px;border-radius:50%">` : ""}
        <span>${esc(p.name)}</span><small>${p.items.toLocaleString("de-DE")} Fotos · ${p.confirmed} bestätigt · ${p.auto} automatisch</small>
        <span style="flex:1"></span>
        <button id="ren">Umbenennen</button><button id="merge">Zusammenführen</button>
        <button id="hide">${p.hidden ? "Einblenden" : "Ausblenden"}</button><button id="del" class="danger">Löschen</button></h1>
      <div class="tabs">
        <a href="#/person/${p.id}" class="${tab === "fotos" ? "active" : ""}">Fotos</a>
        <a href="#/person/${p.id}/vorschlaege" class="${tab === "vorschlaege" ? "active" : ""}">Vorschläge prüfen${p.suggestions ? ` <span class="badge">${p.suggestions}</span>` : ""}</a>
        <a href="#/person/${p.id}/auto" class="${tab === "auto" ? "active" : ""}">Automatisch erkannt (${p.auto})</a>
        <a href="#/person/${p.id}/bestaetigt" class="${tab === "bestaetigt" ? "active" : ""}">Bestätigte Gesichter (${p.confirmed})</a>
      </div>`;
    main.appendChild(page);
    $("#ren", page).onclick = async () => {
      const name = await askName("Neuer Name für " + p.name, p.name);
      if (!name || name === p.name) return;
      try { await api(`/api/persons/${p.id}`, { name }); }
      catch (e) {
        if (e.status === 409 && await confirmBox(`„${name}“ gibt es schon. Beide Personen zusammenführen?`)) {
          await api(`/api/persons/${p.id}/merge`, { into: e.data.other });
          location.hash = "#/person/" + e.data.other;
          return;
        }
      }
      routes.person(id, tab);
    };
    $("#merge", page).onclick = async () => {
      const name = await askName(`${p.name} zusammenführen mit …`, "", true);
      const other = state.persons.find(x => x.name.toLowerCase() === (name || "").toLowerCase());
      if (!other || other.id === p.id) return name && toast("Person nicht gefunden");
      await api(`/api/persons/${p.id}/merge`, { into: other.id });
      location.hash = "#/person/" + other.id;
    };
    $("#hide", page).onclick = async () => { await api(`/api/persons/${p.id}`, { hidden: !p.hidden }); routes.person(id, tab); };
    $("#del", page).onclick = async () => {
      if (!await confirmBox(`Person „${p.name}“ löschen? Die Gesichter bleiben erhalten, nur der Name wird entfernt.`)) return;
      await api(`/api/persons/${p.id}/delete`, {});
      location.hash = "#/personen";
    };
    if (tab === "fotos") {
      const saved = state.chips;
      state.chips = [p.id];
      const params = filterParams();
      state.chips = saved;
      const res = await api("/api/query?" + params);
      const head = document.createElement("div");
      head.className = "gridhead";
      head.innerHTML = `<span class="count">${res.total.toLocaleString("de-DE")} Fotos</span>
        <button class="ghost" id="addchip">In Suche übernehmen</button>${zoomButtons()}`;
      main.appendChild(head);
      bindZoom(head);
      $("#addchip", head).onclick = () => { addChip(p.id); location.hash = "#/fotos"; };
      currentGrid = new VGrid(main, res);
      return;
    }
    const type = { vorschlaege: "sugg", auto: "auto", bestaetigt: "confirmed" }[tab];
    const faces = await api(`/api/persons/${p.id}/faces?type=${type}&limit=600`);
    const help = {
      sugg: `Ist das ${esc(p.name)}? Falsche Gesichter anklicken (werden ausgegraut), dann „Rest bestätigen“.`,
      auto: `Sehr sichere Treffer, die automatisch zugeordnet wurden (unsicherste zuerst). Falsche anklicken und „Markierte entfernen“.`,
      confirmed: `Aus Mylio übernommene und von dir bestätigte Gesichter. Falsche anklicken und „Markierte entfernen“.`,
    }[type];
    const box = document.createElement("div");
    box.className = "page";
    box.style.paddingTop = "0";
    if (!faces.length) {
      box.innerHTML = `<div class="empty">${type === "sugg" ? "Keine offenen Vorschläge. 👍" : "Keine Gesichter."}</div>`;
      main.appendChild(box);
      return;
    }
    const isSugg = type === "sugg";
    box.innerHTML = `<div class="toolbar"><span class="muted">${help}</span><span style="flex:1"></span>
      ${isSugg ? `<button class="primary" id="ok">Rest bestätigen</button><button id="no">Markierte ablehnen</button>`
               : `<button id="no">Markierte entfernen</button>`}
      <button id="other">Markierte anderer Person zuordnen</button></div>
      <div class="faces">${faces.map(f => faceTile(f, isSugg ? f.score : null)).join("")}</div>`;
    main.appendChild(box);
    const sel = new Set();
    bindFaceTiles(box, fid => { sel.has(fid) ? sel.delete(fid) : sel.add(fid); return sel.has(fid) ? (isSugg ? "off" : "sel") : ""; });
    const all = faces.map(f => f.id);
    const reload = () => routes.person(id, tab);
    if (isSugg) $("#ok", box).onclick = async () => {
      const okIds = all.filter(f => !sel.has(f));
      if (okIds.length) await api("/api/faces/assign", { faces: okIds, person: p.id });
      if (sel.size) await api("/api/faces/reject", { faces: [...sel] });
      toast(`${okIds.length} bestätigt` + (sel.size ? `, ${sel.size} abgelehnt` : ""));
      reload();
    };
    $("#no", box).onclick = async () => {
      if (!sel.size) return toast("Erst Gesichter anklicken");
      await api("/api/faces/reject", { faces: [...sel] });
      toast(`${sel.size} entfernt`);
      reload();
    };
    $("#other", box).onclick = async () => {
      if (!sel.size) return toast("Erst Gesichter anklicken");
      const name = await askName("Wer ist das?", "", true);
      if (!name) return;
      await api("/api/faces/assign", { faces: [...sel], name });
      toast(`${sel.size} Gesichter → ${name}`);
      reload();
    };
  },
  async unbekannt(cluster) {
    clearMain();
    await loadPersons();
    const page = document.createElement("div");
    page.className = "page";
    main.appendChild(page);
    if (cluster) {
      const faces = await api(`/api/clusters/${cluster}`);
      page.innerHTML = `<h1><a href="#/unbekannt">Unbekannte Gesichter</a> › Gruppe ${cluster} <small>${faces.length}</small></h1>
        <div class="toolbar"><span class="muted">Falsche Gesichter anklicken (werden ausgegraut), dann Namen eingeben.</span><span style="flex:1"></span>
        ${nameField("cname")}<button class="primary" id="save">Benennen</button><button id="ign">Alle ignorieren</button></div>
        <div class="faces">${faces.map(f => faceTile(f)).join("")}</div>`;
      const off = new Set();
      bindFaceTiles(page, fid => { off.has(fid) ? off.delete(fid) : off.add(fid); return off.has(fid) ? "off" : ""; });
      bindNameField($("#cname", page));
      $("#save", page).onclick = async () => {
        const name = $("#cname input", page).value.trim();
        if (!name) return toast("Bitte Namen eingeben");
        await api("/api/faces/assign", { faces: faces.map(f => f.id).filter(f => !off.has(f)), name });
        toast("Gespeichert");
        location.hash = "#/unbekannt";
      };
      $("#ign", page).onclick = async () => {
        await api("/api/faces/ignore", { faces: faces.map(f => f.id).filter(f => !off.has(f)) });
        location.hash = "#/unbekannt";
      };
      return;
    }
    const clusters = await api("/api/clusters?limit=80");
    page.innerHTML = `<h1><a href="#/personen">Personen</a> › Unbekannte Gesichter <small>${clusters.length} Gruppen</small></h1>
      <p class="muted">Ähnliche Gesichter ohne Namen, nach Häufigkeit sortiert. Namen eingeben (auch bekannte Personen) oder ignorieren (z. B. fremde Leute).
      Danach „Erkennung aktualisieren“ auf der Personen-Seite.</p>
      ${clusters.map(c => `<div class="cluster" data-c="${c.cluster}">
        <div class="faces">${c.faces.map(f => `<div class="face" data-f="${f}"><img loading="lazy" src="/face/${f}" alt=""></div>`).join("")}</div>
        <div class="row"><b>${c.count} Gesichter</b><a href="#/unbekannt/${c.cluster}"><button class="ghost">Alle ansehen</button></a><span style="flex:1"></span>
        ${nameField("n" + c.cluster)}<button class="primary save">Benennen</button><button class="ign">Ignorieren</button></div></div>`).join("")
      || '<div class="empty">Keine unbekannten Gruppen. Nach dem Indexieren hier nochmal schauen.</div>'}`;
    $$(".cluster", page).forEach(el => {
      const c = el.dataset.c;
      bindNameField($(".namefield", el));
      const allFaces = async () => (await api(`/api/clusters/${c}`)).map(f => f.id);
      $(".save", el).onclick = async () => {
        const name = $(".namefield input", el).value.trim();
        if (!name) return toast("Bitte Namen eingeben");
        await api("/api/faces/assign", { faces: await allFaces(), name });
        el.remove();
        toast(`Gruppe als ${name} gespeichert`);
        loadPersons();
      };
      $(".ign", el).onclick = async () => {
        await api("/api/faces/ignore", { faces: await allFaces() });
        el.remove();
      };
    });
  },
  async karte() {
    clearMain();
    main.innerHTML = `<div id="map"></div>`;
    try {
      await loadLeaflet();
    } catch {
      main.innerHTML = `<div class="empty">Die Karte braucht eine Internetverbindung (Kartenkacheln von OpenStreetMap).</div>`;
      return;
    }
    const pts = await api("/api/map?" + filterParams());
    const map = window._map = L.map("map", { preferCanvas: true }).setView([50, 10], 5);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(map);
    const cl = L.markerClusterGroup({ chunkedLoading: true, maxClusterRadius: 50 });
    const ids = pts.map(p => p[0]);
    cl.addLayers(pts.map((p, i) => { const m = L.marker([p[1], p[2]]); m.idx = i; return m; }));
    cl.on("clusterclick", e => {
      if (map.getZoom() < map.getMaxZoom() - 1 && e.layer.getAllChildMarkers().length > 9) return;
      const ms = e.layer.getAllChildMarkers();
      showMapPopup(e.layer.getLatLng(), ms.map(m => m.idx));
    });
    cl.on("click", e => showMapPopup(e.layer.getLatLng(), [e.layer.idx]));
    function showMapPopup(ll, idxs) {
      const sub = idxs.map(i => ids[i]);
      const html = `<div class="mappop">${sub.slice(0, 9).map((id, k) => `<img src="/thumb/${id}" data-k="${k}">`).join("")}</div>` +
        (sub.length > 9 ? `<div style="margin-top:6px">${sub.length} Fotos – Bild anklicken zum Durchblättern</div>` : "");
      const pop = L.popup({ maxWidth: 260 }).setLatLng(ll).setContent(html).openOn(map);
      pop.getElement().querySelectorAll("img").forEach(img => img.onclick = () => openViewer(sub, +img.dataset.k));
    }
    map.addLayer(cl);
    if (pts.length) map.fitBounds(cl.getBounds(), { maxZoom: 12 });
    main.insertAdjacentHTML("beforeend", `<div style="position:absolute;z-index:500;left:60px;top:12px;background:var(--panel);padding:6px 12px;border-radius:8px">${pts.length.toLocaleString("de-DE")} Fotos mit Ort</div>`);
  },
  async import() {
    clearMain();
    const page = document.createElement("div");
    page.className = "page";
    main.appendChild(page);
    page.innerHTML = `<h1>Import <small>neue Fotos auf die Platte holen – bereits vorhandene werden erkannt und übersprungen</small></h1>
      <div class="card" id="jobcard" hidden></div>
      <div class="card"><h2>iCloud Fotos</h2>
        <p class="help">Holt neue Fotos und Videos aus iCloud, auch aus einer <b>geteilten Bibliothek</b> (z. B. wenn Nina ihre Bibliothek mit dir teilt).
        Was schon auf der Platte liegt, wird anhand von Name, Größe, Aufnahmezeit und Bildmaßen erkannt und gar nicht erst heruntergeladen.
        Neue Dateien landen in <b>Bilder/Jahr</b> mit dem Stichwort „iCloud …“.</p>
        <div id="accs"></div>
        <details style="margin-top:10px"><summary style="cursor:pointer">iCloud-Konto hinzufügen</summary>
          <div class="form">
            <label>Name</label><input type="text" id="ic-label" placeholder="z. B. Nina">
            <label>Apple-ID</label><input type="email" id="ic-id" placeholder="name@icloud.com" autocomplete="off">
            <label>Passwort</label><input type="password" id="ic-pw" autocomplete="off">
            <span></span><div class="row"><button class="primary" id="ic-login">Anmelden</button></div>
            <label class="ic-code" hidden>Bestätigungscode</label><input class="ic-code" hidden type="text" id="ic-code" placeholder="6 Ziffern vom iPhone/Mac">
            <span class="ic-code" hidden></span><div class="row ic-code" hidden><button class="primary" id="ic-ok">Code bestätigen</button>
              <button id="ic-again">Code erneut senden</button><span id="ic-phones" class="row"></span></div>
            <span class="ic-code" hidden></span><div class="help ic-code" hidden id="ic-hint"></div>
          </div>
          <p class="help">Das Passwort wird nur zur Anmeldung an Apple geschickt und <b>nicht gespeichert</b>. Apple schickt danach einen Code auf ein Gerät
          des Kontos (bei Ninas Konto also auf Ninas iPhone). Die Anmeldung hält dann meist 1–2 Monate, danach einfach neu anmelden.
          Funktioniert nur, wenn in den iCloud-Einstellungen „Auf iCloud-Daten im Web zugreifen“ an und „Erweiterter Datenschutz“ aus ist.</p>
        </details>
      </div>
      <div class="card"><h2>Ordner oder ZIP – Google Takeout, Amazon Fotos, Handy, SD-Karte …</h2>
        <div class="form">
          <label>Quelle</label><div class="row" style="flex-wrap:nowrap"><input type="text" id="fp" placeholder="Ordner oder .zip wählen" style="flex:1"><button id="fpb">Durchsuchen …</button></div>
          <label>Herkunft</label><input type="text" id="fl" placeholder="z. B. Google Fotos, Amazon Fotos (optional)">
          <label>Alben</label><label style="color:var(--text)"><input type="checkbox" id="fa"> Unterordner als Alben übernehmen (bei Google Takeout automatisch)</label>
          <span></span><div class="row"><button id="fdry">Vorschau</button><button class="primary" id="fgo">Importieren</button></div>
        </div>
        <details class="help"><summary style="cursor:pointer">So bekommst du deine Fotos von Google und Amazon</summary>
          <b>Google Fotos</b> (Google erlaubt seit 2025 keinen direkten Zugriff mehr):
          <ol><li>takeout.google.com öffnen, „Auswahl aufheben“, nur <b>Google Fotos</b> ankreuzen.</li>
          <li>„Exporthäufigkeit“: <b>alle 2 Monate für 1 Jahr</b> – dann kommt der Export automatisch per Mail.</li>
          <li>Dateityp ZIP, Größe 50 GB. Die ZIP-Dateien herunterladen (müssen nicht entpackt werden).</li>
          <li>Hier den Download-Ordner wählen und „Importieren“ – auch bei jedem weiteren Export werden nur neue Fotos übernommen.
          Datum und Ort aus Googles Begleitdateien bleiben erhalten, Google-Alben werden zu Alben.</li></ol>
          <b>Amazon Fotos</b> (keine offizielle Schnittstelle): auf amazon.de/photos Fotos markieren → Herunterladen, oder mit der
          Amazon-Photos-App für Windows/Mac einen Ordner herunterladen lassen. Dann diesen Ordner hier importieren.
        </details>
      </div>
      <div class="card" id="hist" hidden><h2>Bisherige Importe</h2><div></div></div>`;
    $("#fpb", page).onclick = async () => { const p = await browseFs($("#fp", page).value); if (p) $("#fp", page).value = p; };
    const folder = async dry => {
      const path = $("#fp", page).value.trim();
      if (!path) return toast("Bitte erst Ordner oder ZIP wählen");
      try {
        await api("/api/import/folder", { path, label: $("#fl", page).value.trim(), albums: $("#fa", page).checked || null, dry });
        refresh();
      } catch (e) { toast(e.message, 5000); }
    };
    $("#fdry", page).onclick = () => folder(true);
    $("#fgo", page).onclick = () => folder(false);
    let codeFor = null;
    $("#ic-login", page).onclick = async () => {
      const r = await api("/api/icloud/login", { apple_id: $("#ic-id", page).value, password: $("#ic-pw", page).value, label: $("#ic-label", page).value });
      $("#ic-pw", page).value = "";
      if (r.status === "code") {
        codeFor = r.id;
        $$(".ic-code", page).forEach(x => (x.hidden = false));
        $("#ic-phones", page).innerHTML = (r.phones || []).map(ph => `<button data-ph="${ph.id}">Code per SMS an ${esc(ph.number)}</button>`).join("");
        $$("[data-ph]", page).forEach(b => b.onclick = async () => {
          const s = await api("/api/icloud/resend", { id: codeFor, phone: +b.dataset.ph });
          if (s.status === "error") toast(s.error, 6000);
          else { $("#ic-hint", page).textContent = "SMS angefordert – den Code aus der SMS oben eintragen."; toast("SMS angefordert"); }
        });
        $("#ic-hint", page).textContent = r.pushed
          ? "Apple hat den Code an die Geräte des Kontos geschickt (iPhone, iPad, Mac – erscheint als Hinweis „Anmeldeanfrage“, dort „Erlauben“ tippen). Nichts gekommen? „Code erneut senden“ oder per SMS."
          : "Der Code konnte nicht automatisch angefordert werden – bitte „Code per SMS“ wählen.";
        $("#ic-code", page).focus();
      } else if (r.status === "ok") { toast("Angemeldet"); refresh(); }
      else toast(r.error || "Anmeldung fehlgeschlagen", 6000);
    };
    $("#ic-again", page).onclick = async () => {
      const s = await api("/api/icloud/resend", { id: codeFor });
      toast(s.status === "error" ? s.error : s.pushed ? "Neuer Code angefordert" : "Anfordern fehlgeschlagen – bitte SMS wählen", 5000);
    };
    $("#ic-ok", page).onclick = async () => {
      const r = await api("/api/icloud/code", { id: codeFor, code: $("#ic-code", page).value });
      if (r.status === "ok") { toast("Angemeldet"); $$(".ic-code", page).forEach(x => (x.hidden = true)); $("#ic-code", page).value = ""; refresh(); }
      else toast(r.error || "Code falsch", 6000);
    };
    let timer = null;
    const refresh = async () => {
      if (!page.isConnected) return;
      const s = await api("/api/import/status");
      const j = s.job;
      const card = $("#jobcard", page);
      card.hidden = !(j.running || j.finished);
      if (!card.hidden) {
        const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
        card.innerHTML = `<h2>${esc(j.title)} ${j.running ? "" : "– beendet"}</h2>
          ${j.running ? `<p><b>${esc(j.phase)}</b> ${j.total ? `${j.done.toLocaleString("de-DE")} / ${j.total.toLocaleString("de-DE")}` : ""}</p><div class="progress"><i style="width:${pct}%"></i></div>` : ""}
          <div class="stats"><div><b>${j.new.toLocaleString("de-DE")}</b><small>neu</small></div><div><b>${j.existing.toLocaleString("de-DE")}</b><small>schon vorhanden</small></div>
          <div><b>${(j.bytes / 1e9).toFixed(2).replace(".", ",")} GB</b><small>kopiert</small></div><div><b>${j.errors}</b><small>Fehler</small></div></div>
          ${j.message ? `<p>${esc(j.message)}</p>` : ""}${j.running ? `<button id="jstop">Abbrechen</button>` : ""}
          ${j.log.length ? `<div class="log">${j.log.map(esc).join("\n")}</div>` : ""}`;
        const st = $("#jstop", card);
        if (st) st.onclick = () => api("/api/import/stop", {});
      }
      // Kontoliste nur neu zeichnen, wenn sich etwas ändert (sonst gingen Eingaben verloren)
      const accKey = JSON.stringify([s.accounts, j.running]);
      if (accKey !== refresh.accKey) {
      refresh.accKey = accKey;
      $("#accs", page).innerHTML = s.accounts.map(a => `<div class="acc" data-id="${esc(a.id)}"><b>${esc(a.label)}</b><span class="muted">${esc(a.apple_id)}</span>
        <span class="muted">zuletzt: ${a.last_run ? esc(a.last_run.replace("T", " ").slice(0, 16)) : "noch nie"}</span><span style="flex:1"></span>
        ${a.last_run ? "" : `<label class="muted">ab <input type="date" class="since" value="${esc(a.since || "")}"></label>`}
        <button class="primary run" ${j.running ? "disabled" : ""}>Neue holen</button><button class="rm danger">Entfernen</button></div>`).join("")
        || `<p class="muted">Noch kein iCloud-Konto verbunden.</p>`;
      $$(".acc", page).forEach(el => {
        $(".run", el).onclick = async () => {
          const since = $(".since", el) ? $(".since", el).value : "";
          const r = await api("/api/icloud/run", { id: el.dataset.id, since });
          if (r.error) toast(r.error); else refresh();
        };
        $(".rm", el).onclick = async () => {
          if (!await confirmBox("Konto entfernen? Bereits importierte Fotos bleiben natürlich auf der Platte.")) return;
          await api("/api/icloud/remove", { id: el.dataset.id }); refresh();
        };
      });
      }
      const hist = $("#hist", page);
      hist.hidden = !s.sources.length;
      $("div", hist).innerHTML = s.sources.map(x => `<div class="acc"><b>${esc(x.source.replace(/^folder:/, "").replace(/^icloud:/, "iCloud "))}</b>
        <span>${(x.imported || 0).toLocaleString("de-DE")} neu übernommen</span><span class="muted">${(x.existing || 0).toLocaleString("de-DE")} waren schon da</span>
        <span class="muted">zuletzt ${esc((x.last || "").replace("T", " ").slice(0, 16))}</span></div>`).join("");
      const log = $(".log", card);
      if (log) log.scrollTop = log.scrollHeight;
    };
    // Solange die Seite offen ist, laufend nachfragen – schnell während eines Imports
    const loop = async () => {
      if (!page.isConnected) return;
      let running = false;
      try { await refresh(); running = !!$("#jstop", page); } catch { /* nächster Versuch */ }
      clearTimeout(timer);
      timer = setTimeout(loop, running ? 1000 : 4000);
    };
    loop();
  },
  async einstellungen() {
    clearMain();
    const page = document.createElement("div");
    page.className = "page";
    main.appendChild(page);
    const catalogHtml = s => {
      const pr = s.progress;
      const pct = pr.total ? Math.round(100 * pr.done / pr.total) : 0;
      return `<h2>Katalog</h2>
        <div class="stats">
          <div><b>${s.items.toLocaleString("de-DE")}</b><small>Dateien</small></div>
          <div><b>${(s.items - s.pending).toLocaleString("de-DE")}</b><small>verarbeitet</small></div>
          <div><b>${s.faces.toLocaleString("de-DE")}</b><small>Gesichter</small></div>
          <div><b>${s.persons}</b><small>Personen</small></div>
          <div><b>${s.errors}</b><small>nicht lesbar</small></div>
        </div>
        <div class="muted">Bibliothek: ${esc(s.root)} · zuletzt aktualisiert: ${s.last_index ? esc(s.last_index.replace("T", " ")) : "noch nie"}</div>
        ${pr.running ? `<p><b>${esc(pr.phase)}</b> ${pr.total ? `${pr.done.toLocaleString("de-DE")} / ${pr.total.toLocaleString("de-DE")}` : esc(pr.message || "")}
          ${pr.eta ? ` · noch ca. ${fmtEta(pr.eta)}` : ""}${pr.errors ? ` · ${pr.errors} Fehler` : ""}</p>
          <div class="progress"><i style="width:${pct}%"></i></div>
          <p class="muted">Du kannst FotoArchiv währenddessen normal benutzen. Abbrechen ist jederzeit möglich, beim nächsten Start geht es an derselben Stelle weiter.</p>
          <button id="stop">Anhalten</button>`
        : `<p class="muted">Sucht neue, geänderte und gelöschte Dateien. Beim ersten Mal dauert das mehrere Stunden (Vorschaubilder und Gesichter werden erzeugt), danach nur noch Minuten.</p>
          <button class="primary" id="start">Bibliothek aktualisieren</button> <span class="muted">${s.recompute ? "Personenerkennung läuft …" : esc(pr.message || "")}</span>`}
`;
    };
    const bindCatalog = () => {
      const st = $("#start", page), sp = $("#stop", page);
      if (st) st.onclick = async () => { const r = await api("/api/index/start", {}); if (!r.ok) toast(r.error || "Läuft bereits"); setTimeout(refreshCatalog, 500); pollStatus(true); };
      if (sp) sp.onclick = async () => { await api("/api/index/stop", {}); toast("Wird angehalten …"); };
    };
    // während des Einlesens nur die Katalog-Karte auffrischen – der Rest der Seite bleibt stehen
    const refreshCatalog = async () => {
      if (!location.hash.startsWith("#/einstellungen") || !page.isConnected) return;
      const s = await api("/api/status");
      const card = $("#catcard", page);
      if (!card) return;
      card.innerHTML = catalogHtml(s);
      bindCatalog();
      if (s.progress.running || s.recompute) setTimeout(refreshCatalog, 3000);
    };
    const render = async () => {
      const s = await api("/api/status");
      const pr = s.progress;
      const included = new Set(s.included);
      page.innerHTML = `<h1>Bibliothek</h1>
      <div class="card${s.items ? "" : " setup"}"><h2>Fotoordner</h2>
        ${s.items ? "" : `<p><b>Willkommen bei FotoArchiv!</b> Wähle zuerst den Ordner, in dem deine Fotos liegen – z. B. „Bilder“
          oder eine externe Festplatte. Alle Unterordner werden mit durchsucht, die Fotos selbst bleiben, wo sie sind.</p>`}
        <p><code>${esc(s.root)}</code>${s.root_ok ? "" : ` <b style="color:var(--bad)">nicht erreichbar – Platte angesteckt?</b>`}</p>
        ${s.items ? `<p class="muted">Ein anderer Fotoordner geht nur mit neuem Katalog: FotoArchiv beenden und den Ordner „data“
          im FotoArchiv-Ordner löschen (Personen, Alben usw. gehen dabei verloren).</p>`
          : `<button class="primary" id="pickroot">Fotoordner wählen …</button>`}
      </div>
      <div class="card" id="catcard">${catalogHtml(s)}      </div>
      <div class="card"><h2>Ordner in der Bibliothek</h2>
        <p class="muted">Diese Ordner auf der Festplatte werden durchsucht. Nach Änderungen „Bibliothek aktualisieren“.</p>
        <div class="folderlist">${s.all_folders.map(f => `<label><input type="checkbox" value="${esc(f)}" ${included.has(f) ? "checked" : ""}> ${esc(f)}</label>`).join("")}</div>
        <p><label><input type="checkbox" id="hdup" ${s.config.hide_duplicates ? "checked" : ""}> Doppelte Fotos nur einmal zeigen (gleiche Aufnahmezeit und gleicher Bildinhalt)</label><br>
        <label><input type="checkbox" id="hraw" ${s.config.hide_raw_with_jpeg ? "checked" : ""}> RAW-Dateien ausblenden, wenn es ein JPEG/HEIC mit gleichem Namen gibt</label></p>
        <button id="savecfg">Speichern</button>
      </div>
      <div class="card"><h2>Fernseher &amp; Samsung The Frame</h2>
        <p class="muted">Fotos, Diashows und Videos auf dem Fernseher zeigen und Fotos in den Kunstmodus des Frame schicken.
        Der Fernseher muss eingeschaltet und im selben Netz sein.</p>
        <div id="tvset" class="devlist muted">lädt …</div>
        <div class="row"><button id="tvsearch">Fernseher suchen</button><input type="text" id="tvip" placeholder="oder IP-Adresse, z. B. 192.168.178.40" style="width:240px"><button id="tvipok">Übernehmen</button></div>
        <div id="tvfound" class="devlist"></div>
        <h3 style="margin-top:16px">Mit der Fernbedienung des Fernsehers blättern</h3>
        <p class="muted">FotoArchiv erscheint dann am Fernseher als Quelle „FotoArchiv“ (Samsung: Quelle › Verbundene Geräte).
        Dort mit der Fernbedienung durch Ereignisse, Alben, Jahre, Personen und Favoriten gehen, mit ← → blättern, ▶ startet die Diashow.
        Private und ausgeblendete Fotos werden dort nie angezeigt. Funktioniert nur, solange FotoArchiv läuft.</p>
        <label><input type="checkbox" id="dmson"> FotoArchiv im Heimnetz für den Fernseher freigeben</label> <span id="dmsst" class="muted"></span></div>
      <div class="card"><h2>Privat</h2>
        <p class="muted">Fotos, Alben und Ereignisse lassen sich als privat markieren (Fotos markieren › „Mehr …“, oder auf der Album-/Ereignisseite).
        Private Fotos zeigen überall nur ein Schloss, bis das Passwort eingegeben wird. Nach 30 Minuten ohne Nutzung wird automatisch wieder gesperrt.
        Hinweis: Das schützt die Oberfläche – die Dateien selbst auf der Festplatte bleiben unverändert.</p>
        <div class="row">${s.private.password ? `<button id="pwchange">Passwort ändern</button>${s.private.unlocked ? `<button id="pwlock">Jetzt sperren</button>` : `<button id="pwunlock">Entsperren</button>`}`
          : `<button class="primary" id="pwset">Passwort festlegen</button>`}</div></div>
      <div class="card"><h2>Beenden</h2><p class="muted">Vor dem Abziehen der Festplatte FotoArchiv beenden.</p><button id="quit" class="danger">FotoArchiv beenden</button></div>`;
      bindTvSettings(page);
      const pick = $("#pickroot", page);
      if (pick) pick.onclick = async () => {
        const path = await browseFs(s.root);
        if (!path) return;
        try {
          const r = await api("/api/library", { path });
          if (!r.restart) return render();
          toast("Fotoordner gespeichert – FotoArchiv startet neu …", 30000);
          await new Promise(ok => setTimeout(ok, 2000));
          for (let i = 0; i < 60; i++) {
            try { await api("/api/ping"); location.reload(); return; } catch { await new Promise(ok => setTimeout(ok, 1000)); }
          }
          toast("Bitte FotoArchiv neu starten", 8000);
        } catch (e) { toast(e.message, 9000); }
      };
      const pwset = $("#pwset", page), pwch = $("#pwchange", page), pwl = $("#pwlock", page), pwu = $("#pwunlock", page);
      if (pwset) pwset.onclick = async () => { if (await passwordDialog(false)) render(); };
      if (pwch) pwch.onclick = async () => { if (await passwordDialog(true)) render(); };
      if (pwl) pwl.onclick = () => lockNow();
      if (pwu) pwu.onclick = async () => { if (await unlockDialog()) render(); };
      bindCatalog();
      $("#savecfg", page).onclick = async () => {
        const folders = $$(".folderlist input", page).filter(i => i.checked).map(i => i.value);
        await api("/api/config", { folders, hide_duplicates: $("#hdup", page).checked, hide_raw_with_jpeg: $("#hraw", page).checked });
        toast("Gespeichert");
      };
      $("#quit", page).onclick = async () => {
        if (!await confirmBox("FotoArchiv beenden?")) return;
        await api("/api/quit", {});
        document.body.innerHTML = `<div class="empty" style="padding-top:120px">FotoArchiv wurde beendet. Dieses Fenster kann geschlossen werden.</div>`;
      };
      if (pr.running || s.recompute) setTimeout(refreshCatalog, 3000);
    };
    render();
  },
};

// Ordner/ZIP auf dem Computer auswählen (Server listet Laufwerke und Ordner)
function browseFs(start) {
  return new Promise(res => {
    const p = modal(`<div class="fsb"><b>Ordner oder ZIP-Datei wählen</b><div class="row roots"></div>
      <div class="cur"></div><div class="list"></div>
      <div class="row"><button class="primary" id="fsok">Diesen Ordner wählen</button><button id="fsno">Abbrechen</button></div></div>`);
    let cur = "";
    const load = async path => {
      const d = await api("/api/fs?path=" + encodeURIComponent(path || ""));
      cur = d.path;
      $(".roots", p).innerHTML = d.roots.map(r => `<button class="ghost" data-p="${esc(r.path)}">${esc(r.name)}</button>`).join("");
      $(".cur", p).textContent = cur || "Laufwerk oder Ordner wählen";
      $(".list", p).innerHTML = (d.parent ? `<div data-p="${esc(d.parent)}">⬆ eine Ebene höher</div>` : "") +
        d.entries.map(e => `<div data-p="${esc(e.path)}" data-t="${e.type}">${e.type === "zip" ? "🗜" : "📁"} ${esc(e.name)}${e.size ? ` <small class="muted">${fmtSize(e.size)}</small>` : ""}</div>`).join("");
      $$("[data-p]", p).forEach(x => x.onclick = () => {
        if (x.dataset.t === "zip") { closePopover(); res(x.dataset.p); } else load(x.dataset.p);
      });
    };
    $("#fsok", p).onclick = () => { closePopover(); res(cur || null); };
    $("#fsno", p).onclick = () => { closePopover(); res(null); };
    load(start).catch(() => load(""));
  });
}

function fmtRange(a, b) {
  const da = fmtDate(a, false), db = fmtDate(b, false);
  return a.slice(0, 10) === b.slice(0, 10) ? da : `${da} – ${db}`;
}

function fmtEta(s) {
  if (s < 90) return Math.round(s) + " Sek.";
  if (s < 5400) return Math.round(s / 60) + " Min.";
  return (s / 3600).toFixed(1).replace(".", ",") + " Std.";
}

function faceTile(f, score) {
  return `<div class="face" data-f="${f.id}" data-item="${f.item}"><img loading="lazy" src="/face/${f.id}" alt="">
    <button class="open" title="Foto öffnen">⤢</button>${score ? `<small>${Math.round(score * 100)}%</small>` : ""}</div>`;
}
function bindFaceTiles(root, toggle) {
  root.addEventListener("click", e => {
    const t = e.target.closest(".face");
    if (!t || !t.dataset.f) return;
    if (e.target.closest(".open")) { openViewer([+t.dataset.item], 0); return; }
    const cls = toggle(+t.dataset.f);
    t.classList.remove("sel", "off");
    if (cls) t.classList.add(cls);
  });
}

// ------------------------------------------------------- Namensfelder ----
function nameField(id) {
  return `<span class="namefield" id="${id}"><input type="text" placeholder="Name …" autocomplete="off"><div class="dd" hidden></div></span>`;
}
function bindNameField(el, onEnter) {
  const inp = $("input", el), dd = $(".dd", el);
  let sel = -1;
  const update = () => {
    const v = inp.value.trim().toLowerCase();
    const list = state.persons.filter(p => !v || p.name.toLowerCase().includes(v)).slice(0, 8);
    dd.innerHTML = list.map((p, i) => `<div class="${i === sel ? "sel" : ""}">${esc(p.name)}</div>`).join("");
    dd.hidden = !list.length || document.activeElement !== inp;
    $$("div", dd).forEach((d, i) => d.onmousedown = ev => { ev.preventDefault(); inp.value = list[i].name; dd.hidden = true; });
    return list;
  };
  inp.addEventListener("input", () => { sel = -1; update(); });
  inp.addEventListener("focus", update);
  inp.addEventListener("blur", () => setTimeout(() => (dd.hidden = true), 100));
  inp.addEventListener("keydown", e => {
    const list = update();
    if (e.key === "ArrowDown") { sel = Math.min(list.length - 1, sel + 1); update(); e.preventDefault(); }
    if (e.key === "ArrowUp") { sel = Math.max(-1, sel - 1); update(); e.preventDefault(); }
    if (e.key === "Enter") {
      if (sel >= 0 && list[sel]) inp.value = list[sel].name;
      dd.hidden = true;
      if (onEnter) onEnter(inp.value.trim());
    }
    e.stopPropagation();
  });
  return inp;
}

function popover(x, y, html) {
  closePopover();
  const p = document.createElement("div");
  p.className = "popover";
  p.innerHTML = html;
  document.body.appendChild(p);
  p.style.left = Math.min(x, innerWidth - p.offsetWidth - 10) + "px";
  p.style.top = Math.min(y, innerHeight - p.offsetHeight - 10) + "px";
  setTimeout(() => document.addEventListener("mousedown", outside), 0);
  function outside(e) { if (!p.contains(e.target)) closePopover(); }
  p._outside = outside;
  return p;
}
// Dialogfenster: mittig, abgedunkelter Hintergrund, Breite nach Inhalt (Klick daneben oder Esc schließt)
function modal(html) {
  closePopover();
  const bg = document.createElement("div");
  bg.className = "modal-bg";
  const p = document.createElement("div");
  p.className = "popover modal";
  p.innerHTML = html;
  bg.appendChild(p);
  document.body.appendChild(bg);
  p._bg = bg;
  setTimeout(() => document.addEventListener("mousedown", outside), 0);
  function outside(e) { if (!p.contains(e.target) && document.body.contains(e.target)) closePopover(); }
  p._outside = outside;
  return p;
}
function closePopover() {
  $$(".popover").forEach(p => { document.removeEventListener("mousedown", p._outside); (p._bg || p).remove(); });
}
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && $(".modal-bg")) { closePopover(); e.stopPropagation(); }
}, true);
function askName(title, value = "", persons = false) {
  return new Promise(res => {
    const p = modal(`<b>${esc(title)}</b><div class="row">${nameField("askn")}</div>
      <div class="row"><button class="primary" id="askok">OK</button><button id="askno">Abbrechen</button></div>`);
    const inp = bindNameField($("#askn", p), v => { closePopover(); res(v); });
    inp.value = value;
    inp.focus();
    inp.select();
    $("#askok", p).onclick = () => { const v = inp.value.trim(); closePopover(); res(v); };
    $("#askno", p).onclick = () => { closePopover(); res(null); };
  });
}
function confirmBox(text) {
  return new Promise(res => {
    const p = modal(`<div>${esc(text)}</div>
      <div class="row"><button class="primary" id="cy">Ja</button><button id="cn">Abbrechen</button></div>`);
    $("#cy", p).onclick = () => { closePopover(); res(true); };
    $("#cn", p).onclick = () => { closePopover(); res(false); };
  });
}

function confirmCheck(text, label) {
  return new Promise(res => {
    const p = modal(`<div style="max-width:480px">${esc(text)}</div>
      <label class="row" style="max-width:480px"><input type="checkbox" id="cc"> ${esc(label)}</label>
      <div class="row"><button class="primary" id="cy">Löschen</button><button id="cn">Abbrechen</button></div>`);
    $("#cy", p).onclick = () => { const checked = $("#cc", p).checked; closePopover(); res({ checked }); };
    $("#cn", p).onclick = () => { closePopover(); res(null); };
  });
}
function toastAction(msg, label, fn) {
  toast(msg, 8000);
  const b = document.createElement("button");
  b.className = "ghost";
  b.style.marginLeft = "12px";
  b.textContent = label;
  b.onclick = () => { $("#toast").hidden = true; fn(); };
  $("#toast").appendChild(b);
}
function reportErrors(r) {
  if (r.errors && r.errors.length) toast(`${r.errors.length} Datei(en) nicht möglich, z. B. ${r.errors[0]}`, 9000);
}
// Fotos/Videos in den Papierkorb (Dateien werden auf der Platte nur verschoben)
async function trashItems(ids, ask = true) {
  const n = ids.length;
  if (ask && !await confirmBox(`${n === 1 ? "Dieses Element" : n.toLocaleString("de-DE") + " Fotos/Videos"} in den Papierkorb legen? ` +
    "Die Dateien wandern auf der Platte in den Ordner „FotoArchiv-Papierkorb“ und lassen sich wiederherstellen, bis du den Papierkorb leerst. " +
    "Ausgeblendete Duplikate und RAW-Partner kommen mit.")) return null;
  const r = await api("/api/items/delete", { ids });
  reportErrors(r);
  loadYears().catch(() => {});
  return r;
}
async function undoTrash(r, after) {
  const u = await api("/api/trash/restore", { ids: r.ids });
  reportErrors(u);
  toast(`${u.count.toLocaleString("de-DE")} wiederhergestellt`);
  loadYears().catch(() => {});
  if (after) after();
}

async function recompute() {
  const r = await api("/api/faces/recompute", {});
  toast(r.ok ? "Erkennung läuft im Hintergrund (ca. 1–3 Minuten) …" : "Läuft gerade schon oder Index wird erstellt");
  if (r.ok) pollStatus(true);
}

// ------------------------------------------------------------ Betrachter ----
const viewer = {
  el: $("#viewer"), ids: [], i: 0, item: null, showFaces: localStorageGet("faces") === "1", panel: localStorageGet("panel") === "1",
};

function openViewer(ids, i) {
  viewer.ids = ids;
  viewer.i = i;
  viewer.el.hidden = false;
  viewer.el.classList.toggle("panel-open", viewer.panel);
  $(".v-panel", viewer.el).hidden = !viewer.panel;
  $(".v-faces", viewer.el).classList.toggle("on", viewer.showFaces);
  showItem();
}
function closeViewer() {
  if (viewer.tvFollow) stopTvFollow(false);
  viewer.el.hidden = true;
  $(".v-media", viewer.el).innerHTML = "";
  closePopover();
  if (currentGrid && currentGrid.ids === viewer.ids) currentGrid.scrollToIndex(viewer.i);
  if (viewer.dirty) {  // im Betrachter gelöscht/wiederhergestellt: Raster neu laden
    viewer.dirty = false;
    const i = viewer.i;
    route().then(() => currentGrid && currentGrid.scrollToIndex(Math.min(i, currentGrid.ids.length - 1)));
  }
}
async function viewerDelete() {
  const it = viewer.item;
  if (!it || it.locked) return;
  if (it.hidden === 2) {
    const r = await api("/api/trash/restore", { ids: [it.id] });
    reportErrors(r);
    if (!r.count) return;
    toast("Wiederhergestellt");
  } else {
    const r = await trashItems([it.id], false);
    if (!r.count) return;
    toastAction("In den Papierkorb gelegt", "Rückgängig", () => undoTrash(r, () => { if (viewer.el.hidden) route(); else viewer.dirty = true; }));
  }
  viewer.dirty = true;
  viewer.ids = viewer.ids.filter(x => x !== it.id);
  if (!viewer.ids.length) return closeViewer();
  viewer.i = Math.min(viewer.i, viewer.ids.length - 1);
  showItem();
}
async function showItem() {
  const id = viewer.ids[viewer.i];
  const media = $(".v-media", viewer.el);
  $(".v-prev", viewer.el).hidden = viewer.i <= 0;
  $(".v-next", viewer.el).hidden = viewer.i >= viewer.ids.length - 1;
  const it = viewer.item = await api("/api/item/" + id);
  if (viewer.ids[viewer.i] !== id) return;
  if (viewer.tvFollow && !it.locked) sendTvCurrent();
  if (it.locked) {
    $(".v-title", viewer.el).textContent = "Privat" + (viewer.ids.length > 1 ? ` · ${viewer.i + 1}/${viewer.ids.length}` : "");
    media.innerHTML = `<div class="lockscreen"><div class="big">🔒</div><p>Dieses Foto ist privat.</p><button class="primary" id="vunlock">Entsperren …</button></div>`;
    $("#vunlock", media).onclick = async () => { if (await unlockDialog()) showItem(); };
    $(".v-panel", viewer.el).innerHTML = "";
    return;
  }
  $(".v-title", viewer.el).textContent = `${fmtDate(it.taken)} · ${it.name}` + (viewer.ids.length > 1 ? ` · ${viewer.i + 1}/${viewer.ids.length}` : "");
  $(".v-fav", viewer.el).textContent = it.fav ? "★" : "☆";
  $(".v-fav", viewer.el).classList.toggle("on", !!it.fav);
  $(".v-edit", viewer.el).hidden = it.kind === "video";
  $(".v-vadd", viewer.el).hidden = false;
  $(".v-edit", viewer.el).classList.toggle("on", !!it.edit);
  if (it.kind === "video") {
    // MP4/MOV spielt der Browser direkt; MTS, AVI, WMV … werden einmalig umgepackt (Fortschritt sichtbar)
    const direct = ["mp4", "m4v", "mov", "qt", "webm"].includes(it.ext);
    const prepare = async () => {
      media.innerHTML = `<div class="fallback"><img src="/thumb/${id}"><p class="vprep">Video wird für den Browser vorbereitet …</p>
        <div class="progress" style="width:320px;margin:0 auto"><i style="width:0%"></i></div>
        <p class="muted" style="font-size:12px">Nur beim ersten Öffnen nötig.</p>
        <button onclick="openExternal(${id})">Stattdessen mit Standard-Programm öffnen</button></div>`;
      let s = await api(`/api/video/${id}/prepare`, {});
      while (s.state === "running" && viewer.ids[viewer.i] === id && !viewer.el.hidden) {
        $(".progress i", media).style.width = Math.round(100 * s.progress) + "%";
        $(".vprep", media).textContent = `Video wird für den Browser vorbereitet … ${Math.round(100 * s.progress)} %`;
        await new Promise(r => setTimeout(r, 1000));
        s = await api(`/api/video/${id}/status`);
      }
      if (viewer.ids[viewer.i] !== id || viewer.el.hidden) return;
      if (s.state === "done") {
        media.innerHTML = `<video controls autoplay playsinline poster="/thumb/${id}" src="/video/${id}"></video>`;
        rotateVideo($("video", media), it.userrot);
      } else {
        media.innerHTML = `<div class="fallback"><img src="/thumb/${id}"><p>Dieses Video lässt sich im Browser nicht abspielen${s.error ? ` (${esc(s.error)})` : ""}.</p>
          <button onclick="openExternal(${id})">Mit Standard-Programm öffnen</button></div>`;
      }
    };
    if (direct) {
      media.innerHTML = `<video controls autoplay playsinline poster="/thumb/${id}" src="/original/${id}"></video>`;
      rotateVideo($("video", media), it.userrot);
      $("video", media).onerror = prepare;   // z. B. HEVC ohne Hardware-Unterstützung
    } else {
      prepare();
    }
  } else {
    const src = it.browser_ok ? `/original/${id}` : `/preview/${id}` + (thumbVer[id] ? `?v=${thumbVer[id]}` : "");
    media.innerHTML = `<div class="wrap"><img src="${thumbUrl(id)}" alt="" class="lo"></div>`;
    const img = new Image();
    img.onload = () => {
      if (viewer.ids[viewer.i] !== id) return;
      const wrap = $(".wrap", media);
      wrap.innerHTML = "";
      wrap.appendChild(img);
      drawFaces();
    };
    img.src = src;
    const lo = $("img.lo", media);
    lo.style.cssText = "width:min(100vw,calc((100vh - 48px) * " + ((it.width || 4) / (it.height || 3)) + "));filter:blur(0px)";
  }
  renderPanel();
  // Nachbarn vorladen
  for (const k of [viewer.i + 1, viewer.i - 1]) {
    const nid = viewer.ids[k];
    if (nid) new Image().src = `/thumb/${nid}`;
  }
}
function drawFaces() {
  const media = $(".v-media", viewer.el);
  $$(".fbox", media).forEach(b => b.remove());
  if (!viewer.showFaces || !viewer.item || viewer.item.kind === "video") return;
  const img = $(".wrap img", media);
  if (!img) return;
  const wrap = $(".wrap", media);
  if (!img.naturalWidth) return;
  // Rahmen auf die tatsächlich sichtbare Bildfläche beziehen (nicht auf den umgebenden Kasten,
  // der bei Hochkantfotos breiter ist als das Bild)
  const r = img.getBoundingClientRect(), wr = wrap.getBoundingClientRect();
  const s = Math.min(r.width / img.naturalWidth, r.height / img.naturalHeight);
  const W = img.naturalWidth * s, H = img.naturalHeight * s;
  const L = r.left - wr.left + (r.width - W) / 2, T = r.top - wr.top + (r.height - H) / 2;
  // Bearbeitetes Foto: Rahmen wie das Bild spiegeln, begradigen (Zoom) und zuschneiden
  const ed_ = viewer.item.edit || {}, cr = ed_.crop || [0, 0, 1, 1];
  const zz = ed_.angle && viewer.item.width ? zoomForAngle(viewer.item.width, viewer.item.height, ed_.angle) : 1;
  const mapX = (x, w) => (0.5 + ((ed_.flip ? 1 - x - w : x) - 0.5) * zz - cr[0]) / cr[2];
  const mapY = y => (0.5 + (y - 0.5) * zz - cr[1]) / cr[3];
  for (const f0 of viewer.item.faces) {
    if (f0.x == null) continue;
    const f = Object.assign({}, f0, { x: mapX(f0.x, f0.w), y: mapY(f0.y), w: f0.w * zz / cr[2], h: f0.h * zz / cr[3] });
    if (f.x + f.w < 0 || f.y + f.h < 0 || f.x > 1 || f.y > 1) continue;
    const b = document.createElement("div");
    const known = f.person != null;
    b.className = "fbox" + (known ? "" : " unk");
    b.style.cssText = `left:${L + f.x * W}px;top:${T + f.y * H}px;width:${f.w * W}px;height:${f.h * H}px`;
    b.innerHTML = `<span>${esc(known ? f.name + (f.source === "auto" ? " ?" : "") : f.sugg_name ? f.sugg_name + "?" : "Wer ist das?")}</span>`;
    b.onclick = e => { e.stopPropagation(); faceMenu(f, e.clientX, e.clientY); };
    wrap.appendChild(b);
  }
}
// bei Fenstergröße/Info-Leiste die Rahmen neu ausrichten
window.addEventListener("resize", () => { if (!viewer.el.hidden) drawFaces(); });
new ResizeObserver(() => { if (!viewer.el.hidden) drawFaces(); }).observe($(".v-media"));
function faceMenu(f, x, y) {
  const known = f.person != null;
  const p = popover(x, y, `<div class="row"><img src="/face/${f.id}" style="width:48px;height:48px;border-radius:6px">
      <b>${known ? esc(f.name) : f.sugg_name ? "Vorschlag: " + esc(f.sugg_name) : "Unbekannt"}</b></div>
    <div class="row">${nameField("fm")}<button class="primary" id="fmok">OK</button></div>
    <div class="row">${!known && f.sugg_name ? `<button id="fmyes">Ja, ${esc(f.sugg_name)}</button>` : ""}
      ${known && f.source === "auto" ? `<button id="fmyes">Bestätigen</button>` : ""}
      ${known || f.sugg_name ? `<button id="fmno">Falsch</button>` : ""}<button id="fmign" title="z. B. fremde Person">Ignorieren</button></div>`);
  const done = async (fn) => { closePopover(); await fn; await reloadItem(); };
  const inp = bindNameField($("#fm", p), v => v && done(api("/api/faces/assign", { faces: [f.id], name: v })));
  inp.focus();
  $("#fmok", p).onclick = () => inp.value.trim() && done(api("/api/faces/assign", { faces: [f.id], name: inp.value.trim() }));
  const yes = $("#fmyes", p);
  if (yes) yes.onclick = () => done(api("/api/faces/assign", { faces: [f.id], person: f.person || f.sugg }));
  const no = $("#fmno", p);
  if (no) no.onclick = () => done(api("/api/faces/reject", { faces: [f.id] }));
  $("#fmign", p).onclick = () => done(api("/api/faces/ignore", { faces: [f.id] }));
}
async function reloadItem() {
  viewer.item = await api("/api/item/" + viewer.ids[viewer.i]);
  await loadPersons();
  drawFaces();
  renderPanel();
}
function renderPanel() {
  const panel = $(".v-panel", viewer.el);
  if (!viewer.panel || !viewer.item) return;
  const it = viewer.item;
  const named = it.faces.filter(f => f.person != null);
  const unknown = it.faces.filter(f => f.person == null && f.x != null);
  const srcLabel = { exif: "Kamera", xmp: "Mylio/XMP", mylio: "aus Mylio übernommen", manual: "von dir geändert", video: "Video",
    name: "Dateiname", file: "Dateidatum (unsicher)", undated: "in Mylio undatiert" }[it.taken_src] || "";
  panel.innerHTML = `<h3>${esc(fmtDate(it.taken))}</h3><div class="muted">${esc(srcLabel)}</div>
    <dl>
      ${it.place ? `<dt>Ort</dt><dd>${esc(it.place)}</dd>` : ""}
      ${it.lat != null ? `<dt>GPS</dt><dd><a href="https://www.openstreetmap.org/?mlat=${it.lat}&mlon=${it.lon}#map=15/${it.lat}/${it.lon}" target="_blank">${it.lat.toFixed(5)}, ${it.lon.toFixed(5)}</a></dd>` : ""}
      ${it.camera ? `<dt>Kamera</dt><dd>${esc(it.camera)}</dd>` : ""}
      <dt>Größe</dt><dd>${it.width ? `${it.width} × ${it.height}` : ""} ${fmtSize(it.size)}${it.duration ? ` · ${Math.round(it.duration)} s` : ""}</dd>
      ${it.rating ? `<dt>Bewertung</dt><dd>${"★".repeat(it.rating)}</dd>` : ""}
      ${it.keywords ? `<dt>Stichwörter</dt><dd>${esc(it.keywords)}</dd>` : ""}
      ${it.caption ? `<dt>Beschreibung</dt><dd>${esc(it.caption)}</dd>` : ""}
      ${it.events.length ? `<dt>Ereignis</dt><dd>${it.events.map(e => `<a class="tag" href="#/ereignis/${e.id}" onclick="closeViewer()">${esc(e.name)}</a>`).join("")}</dd>` : ""}
      <dt>Alben</dt><dd>${it.albums.map(a => `<span class="tag"><a href="#/album/${a.id}" onclick="closeViewer()">${esc(a.name)}</a><span class="x" data-ralbum="${a.id}" title="Aus Album entfernen">✕</span></span>`).join("")}<button class="addbtn" id="vp-album">+ Album</button></dd>
      <dt>Stichwörter</dt><dd>${(it.usertags || "").split(", ").filter(Boolean).map(t => `<span class="tag">${esc(t)}<span class="x" data-rtag="${esc(t)}" title="Entfernen">✕</span></span>`).join("")}<button class="addbtn" id="vp-tag">+ Stichwort</button></dd>
      <dt>Datei</dt><dd><a href="#/ordner/${encodeURIComponent(it.folder)}" onclick="closeViewer()">${esc(it.folder)}</a>/${esc(it.name)}</dd>
      ${it.copies.length ? `<dt>Kopien</dt><dd>${it.copies.map(esc).join("<br>")}</dd>` : ""}
      ${it.error ? `<dt>Fehler</dt><dd>${esc(it.error)}</dd>` : ""}
    </dl>
    <div class="v-acts"><button onclick="openExternal(${it.id})">Öffnen mit …</button><button onclick="revealExternal(${it.id})">Im Ordner zeigen</button>
      <button id="vp-date">Datum ändern</button><button id="vp-person">+ Person</button>
      <button id="vp-hide">${it.hidden ? "Wieder einblenden" : "Ausblenden"}</button>
      <button id="vp-priv">${it.private ? "Privat aufheben" : "🔒 Privat"}</button>
      ${it.kind !== "video" ? `<button id="vp-edit">✎ ${it.edit ? "Bearbeitung ändern" : "Bearbeiten"}</button>` : ""}
      <button id="vp-del" class="danger">${it.hidden === 2 ? "Wiederherstellen" : "🗑 Löschen"}</button></div>
    ${it.hidden === 2 ? `<div class="muted" style="margin-top:6px">🗑 Im Papierkorb seit ${esc(fmtDate(it.trashed))}, vorher ${esc(it.trash_from)}</div>` : ""}
    ${it.priv_eff && !it.private ? `<div class="muted" style="margin-top:6px">🔒 Privat über Album oder Ereignis.</div>` : ""}
    <h3 style="margin-top:18px">Personen</h3>
    ${named.map(f => `<div class="pf">${f.x != null ? `<img src="/face/${f.id}">` : ""}<a href="#/person/${f.person}" onclick="closeViewer()">${esc(f.name)}</a>
      ${f.source === "auto" ? '<span class="sg">automatisch</span>' : ""}</div>`).join("") || '<div class="muted">keine</div>'}
    ${unknown.length ? `<div class="muted" style="margin-top:8px">${unknown.length} unbekannte${unknown.length === 1 ? "s Gesicht" : " Gesichter"} – Taste G zeigt Rahmen zum Benennen.</div>` : ""}`;
  bindPanel(panel, it);
}
function bindPanel(panel, it) {
  const ids = [it.id];
  const after = async msg => { if (msg) toast(msg); await reloadItem(); };
  $("#vp-album", panel).onclick = async () => { if (await actAlbum(ids)) after(); };
  $("#vp-tag", panel).onclick = async () => { if (await actTag(ids)) after(); };
  $("#vp-date", panel).onclick = async () => { if (await actDate(ids, it.taken)) after("Datum geändert"); };
  $("#vp-person", panel).onclick = async () => { if (await actPerson(ids)) after(); };
  $("#vp-hide", panel).onclick = async () => {
    await api("/api/items/hide", { ids, hidden: !it.hidden });
    after(it.hidden ? "Wieder eingeblendet" : "Ausgeblendet – zu finden unter Alben › Ausgeblendete Fotos");
  };
  $("#vp-del", panel).onclick = () => viewerDelete();
  const vpe = $("#vp-edit", panel);
  if (vpe) vpe.onclick = () => openEditor();
  $("#vp-priv", panel).onclick = async () => {
    if (!it.private && !await ensurePassword()) return;
    await api("/api/items/private", { ids, private: !it.private });
    after(it.private ? "Nicht mehr privat" : "Als privat markiert");
  };
  $$("[data-ralbum]", panel).forEach(x => x.onclick = async () => { await api(`/api/albums/${x.dataset.ralbum}/remove`, { ids }); after(); });
  $$("[data-rtag]", panel).forEach(x => x.onclick = async () => { await api("/api/items/tags", { ids, remove: [x.dataset.rtag] }); after(); });
}

// ------------------------------------------------------ Aktionen (Auswahl) ----
const selbar = $("#selbar");
let selGrid = null;
function updateSelbar(grid) {
  selGrid = grid && grid.sel.size ? grid : null;
  selbar.hidden = !selGrid;
  if (!selGrid) return;
  $(".sb-count", selbar).textContent = `${selGrid.sel.size.toLocaleString("de-DE")} ausgewählt`;
}
$(".sb-close", selbar).onclick = () => selGrid && selGrid.clearSel();
$(".sb-all", selbar).onclick = () => selGrid && selGrid.selectAll();
$$("[data-act]", selbar).forEach(b => b.onclick = e => runAction(b.dataset.act, e));
document.addEventListener("keydown", e => {
  if (!viewer.el.hidden || e.target.matches("input, textarea, select")) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a" && currentGrid) { e.preventDefault(); currentGrid.selectAll(); }
  else if (e.key === "Escape" && selGrid && !$(".popover")) selGrid.clearSel();
  else if (e.key === "Delete" && selGrid && !$(".popover")) runAction(selGrid.context.trash ? "purge" : "del");
});

async function runAction(act, ev) {
  const g = selGrid;
  if (!g) return;
  const ids = g.selectedIds(), n = ids.length;
  const done = (msg, reload) => { toast(msg); g.clearSel(); if (reload) route(); };
  if (act === "del") {
    const r = await trashItems(ids);
    if (!r) return;
    done("", true);
    toastAction(`${r.count.toLocaleString("de-DE")} in den Papierkorb gelegt`, "Rückgängig", () => undoTrash(r, route));
    return;
  }
  if (act === "restore") {
    const r = await api("/api/trash/restore", { ids });
    done(`${r.count.toLocaleString("de-DE")} wiederhergestellt`, true);
    reportErrors(r);
    return loadYears().catch(() => {});
  }
  if (act === "purge") {
    if (!await confirmBox(`${n.toLocaleString("de-DE")} Dateien endgültig von der Platte löschen? Das lässt sich nicht rückgängig machen.`)) return;
    const r = await api("/api/trash/purge", { ids });
    done(`${r.count.toLocaleString("de-DE")} endgültig gelöscht`, true);
    return reportErrors(r);
  }
  if (act === "video") {
    const r = await addToVideo(ids);
    if (!r) return;
    done("", false);
    toastAction(`${n} in „${r.name}“ übernommen`, "Öffnen", () => { location.hash = "#/video/" + r.id; });
    return;
  }
  if (act === "show") return slideshowDialog(ids, `${n} Fotos`);
  if (act === "share") return shareDialog(ids, g.context.albumName || "");
  if (act === "fav") {
    await api("/api/items/fav", { ids, fav: true });
    return done(`${n} als Favorit markiert`);
  }
  if (act === "album") { const r = await actAlbum(ids); if (r) done(`${n} Fotos → ${r}`); return; }
  if (act === "tag") { const r = await actTag(ids); if (r) done(`Stichwort „${r}“ bei ${n} Fotos`); return; }
  if (act === "person") { const r = await actPerson(ids); if (r) done(`${r} bei ${n} Fotos eingetragen`); return; }
  if (act === "event") {
    const name = await askText(`Ereignis aus ${n} Fotos erstellen – Name:`, "");
    if (!name) return;
    try {
      const r = await api("/api/events", { name, ids });
      done(`Ereignis „${name}“ erstellt`);
      location.hash = "#/ereignis/" + r.id;
    } catch (e) { toast(e.message); }
    return;
  }
  if (act === "more") {
    const ctx = g.context;
    const items = [
      ["unfav", "☆ Favorit entfernen"],
      ["rotr", "⟳ Nach rechts drehen"], ["rotl", "⟲ Nach links drehen"],
      ["date", "Datum ändern …"],
      ["rmperson", "Person entfernen …"],
      ["rmtag", "Stichwort entfernen …"],
      "-",
      ctx.album ? ["rmalbum", `Aus Album „${ctx.albumName}“ entfernen`] : null,
      ctx.album && n === 1 ? ["cover", "Als Titelbild des Albums"] : null,
      ctx.trash ? null : ctx.hidden ? ["unhide", "Wieder einblenden"] : ["hide", "Ausblenden (nicht löschen)"],
      ctx.trash ? ["restore", "Wiederherstellen"] : ["del", "🗑 In den Papierkorb …"],
      ctx.trash ? ["purge", "Endgültig löschen …"] : null,
      "-",
      ["priv", "🔒 Als privat markieren"], ["unpriv", "Privat aufheben"],
    ].filter(Boolean);
    const r = ev.target.getBoundingClientRect();
    const p = popover(r.right - 230, r.bottom + 6, `<div class="menu">${items.map(x => x === "-" ? "<hr>" : `<div data-m="${x[0]}">${esc(x[1])}</div>`).join("")}</div>`);
    $$("[data-m]", p).forEach(d => d.onclick = async () => {
      closePopover();
      const m = d.dataset.m;
      if (m === "del" || m === "restore" || m === "purge") return runAction(m);
      if (m === "unfav") { await api("/api/items/fav", { ids, fav: false }); done(`Favorit bei ${n} Fotos entfernt`, ctx.fav); }
      else if (m === "rotr" || m === "rotl") {
        toast(`${n} Fotos werden gedreht …`, 6000);
        await api("/api/items/rotate", { ids, dir: m === "rotr" ? "cw" : "ccw" });
        ids.forEach(id => (thumbVer[id] = Date.now()));
        done("Gedreht");
        g.layout();
      }
      else if (m === "date") { if (await actDate(ids)) done("Datum geändert", true); }
      else if (m === "rmperson") {
        const name = await askName("Welche Person entfernen?", "", true);
        const pers = state.persons.find(x => x.name.toLowerCase() === (name || "").toLowerCase());
        if (pers) { await api("/api/items/person", { ids, person: pers.id, remove: true }); done(`${pers.name} bei ${n} Fotos entfernt`); }
      }
      else if (m === "rmtag") {
        const tags = await api("/api/tags");
        const t = await pickFrom("Welches Stichwort entfernen?", tags.map(([t, c]) => ({ id: t, label: t, note: c })), { allowNew: false });
        if (t) { await api("/api/items/tags", { ids, remove: [t.id] }); done(`„${t.id}“ entfernt`); }
      }
      else if (m === "rmalbum") { await api(`/api/albums/${ctx.album}/remove`, { ids }); done(`${n} aus dem Album entfernt`, true); }
      else if (m === "cover") { await api(`/api/albums/${ctx.album}`, { cover: ids[0] }); done("Titelbild gesetzt"); }
      else if (m === "priv" || m === "unpriv") {
        if (m === "priv" && !await ensurePassword()) return;
        if (m === "unpriv" && !state.priv.unlocked && !await unlockDialog()) return;
        await api("/api/items/private", { ids, private: m === "priv" });
        done(m === "priv" ? `${n} Fotos sind jetzt privat` : `${n} Fotos sind nicht mehr privat`, true);
      }
      else if (m === "hide" || m === "unhide") {
        await api("/api/items/hide", { ids, hidden: m === "hide" });
        done(m === "hide" ? `${n} ausgeblendet (zu finden unter Alben › Ausgeblendete Fotos)` : `${n} wieder eingeblendet`, true);
      }
    });
  }
}

// ------------------------------------------------------------- Diashow ----
async function slideshowDialog(ids, title) {
  const m = await api("/api/music");
  const p = modal(`<div class="dlg"><b>Diashow: ${esc(title)}</b> <span class="muted">(${ids.length.toLocaleString("de-DE")})</span>
    <div class="form">
      <label>Dauer</label><select id="sd"><option value="3">3 Sekunden</option><option value="5" selected>5 Sekunden</option><option value="8">8 Sekunden</option></select>
      <label>Effekt</label><select id="se"><option value="kb">Kamerafahrt (Ken Burns)</option><option value="fade">Nur überblenden</option></select>
      <label>Reihenfolge</label><select id="so"><option value="">wie angezeigt</option><option value="shuffle">zufällig</option></select>
      <label>Musik</label><select id="sm"><option value="">ohne Musik</option>
        ${m.music.length ? `<option value="__all">alle Titel (${m.music.length})</option>` : ""}
        ${m.music.map((x, i) => `<option value="${i}">${esc(x.name)}</option>`).join("")}<option value="__file">eigene Datei wählen …</option></select>
    </div>
    <input type="file" id="sf" accept="audio/*" multiple hidden>
    <div class="help">${m.music.length ? "" : "Tipp: MP3/M4A-Dateien in den Ordner <b>FotoArchiv › Musik</b> legen, dann stehen sie hier zur Auswahl. "}Videos laufen ganz mit Ton, die Musik pausiert so lange.
    Während der Diashow: ← → blättern, Leertaste Pause.</div>
    <div class="row"><button class="primary" id="sgo">▶ Abspielen</button><button id="stv" title="Foto für Foto auf dem Fernseher (ohne Musik)">📺 Auf dem Fernseher</button>
      <button id="smir" title="PC-Bildschirm auf den Fernseher spiegeln (Win+K) – Diashow mit Musik, Effekten und Videos">🖥 Bildschirm spiegeln</button></div>
    <div class="row"><button id="svid" title="Als MP4-Video (1080p) auf den Schreibtisch">Als Video mit Musik speichern</button>
      <label class="muted"><input type="checkbox" id="svtv"> danach auf dem Fernseher abspielen</label><button id="sno">Abbrechen</button></div></div>`);
  let files = null;
  $("#sm", p).onchange = e => { if (e.target.value === "__file") $("#sf", p).click(); };
  $("#sf", p).onchange = e => { files = [...e.target.files]; if (!files.length) $("#sm", p).value = ""; };
  const opts = () => {
    const v = $("#sm", p).value;
    const tracks = v === "__all" ? m.music : v !== "" && v !== "__file" ? [m.music[+v]] : [];
    let order = [...ids];
    if ($("#so", p).value === "shuffle") order.sort(() => Math.random() - 0.5);
    return { ids: order, seconds: +$("#sd", p).value, kb: $("#se", p).value === "kb", tracks, files: v === "__file" ? files : null };
  };
  $("#sno", p).onclick = closePopover;
  $("#smir", p).onclick = async () => {
    try { await api("/api/tv/mirror", {}); toast("Rechts unten den Fernseher anklicken, dann hier „▶ Abspielen“", 8000); }
    catch (e) { toast(e.message, 8000); }
  };
  $("#sgo", p).onclick = () => { const o = opts(); closePopover(); playSlideshow(o); };
  $("#stv", p).onclick = async () => {
    const o = opts();
    closePopover();
    // direkt Foto für Foto (Musik/Effekte gibt es dort nur über „Als Video mit Musik speichern“)
    await startTvShow({ ids: o.ids, seconds: Math.max(4, o.seconds), shuffle: false, videos: true, title, mode: "direct",
      loop: true });
  };
  $("#svid", p).onclick = async () => {
    const o = opts();
    if (o.files) return toast("Für das Video bitte Musik aus dem Ordner FotoArchiv › Musik wählen", 5000);
    closePopover();
    const tv = $("#svtv", p).checked;
    const r = await api("/api/slideshow/video", { ids: o.ids, seconds: o.seconds, kenburns: o.kb, music: o.tracks.map(t => t.path), name: title, tv });
    if (r.error) return toast(r.error);
    toast("Video wird erstellt …", 4000);
    pollShare();
  };
}

function playSlideshow(o) {
  const ids = window.lockedIds && window.lockedIds.size && !state.priv.unlocked ? o.ids.filter(i => !window.lockedIds.has(i)) : o.ids;
  if (!ids.length) return;
  const el = document.createElement("div");
  el.id = "show";
  el.innerHTML = `<div class="sl"></div><div class="sl"></div><div class="cap"></div>
    <div class="bar"><button data-k="prev">‹</button><button data-k="pause">❚❚</button><button data-k="next">›</button>
    <span class="muted pos"></span><span style="flex:1"></span><button data-k="close">✕ Beenden (Esc)</button></div>`;
  document.body.appendChild(el);
  const layers = $$(".sl", el);
  const audio = new Audio();
  let playlist = o.files ? o.files.map(f => URL.createObjectURL(f)) : o.tracks.map(t => "/music?f=" + encodeURIComponent(t.path));
  let track = 0, i = -1, front = 0, paused = false, timer = null, uiTimer = null;
  const nextTrack = () => { if (!playlist.length) return; audio.src = playlist[track % playlist.length]; track++; audio.play().catch(() => {}); };
  audio.addEventListener("ended", nextTrack);
  audio.volume = 0.9;
  nextTrack();
  // Infos je Foto/Video (Art, Format) – einmal holen, für Bildunterschrift und Videos
  const info = {};
  const itemInfo = id => info[id] || (info[id] = api("/api/item/" + id).catch(() => null));
  const DIRECT = ["mp4", "m4v", "mov", "qt", "webm"];
  const preload = k => {
    const id = ids[(k + ids.length) % ids.length];
    if (!id) return;
    itemInfo(id).then(it => {
      if (it && it.kind === "video") {
        if (!DIRECT.includes(it.ext)) api(`/api/video/${id}/prepare`, {}).catch(() => {});  // MTS/AVI rechtzeitig umpacken
      } else new Image().src = "/screen/" + id;
    });
  };
  let seq = 0, video = null;
  const musicPause = () => { if (playlist.length) audio.pause(); };
  const musicResume = () => { if (playlist.length && !paused) audio.play().catch(() => {}); };
  const stopVideo = () => {
    if (!video) return;
    const v = video;
    video = null;
    v.onended = v.onerror = null;
    setTimeout(() => { v.pause(); v.removeAttribute("src"); v.load(); }, 1200);  // nach dem Überblenden
    musicResume();
  };
  const videoSrc = async it => {
    if (DIRECT.includes(it.ext)) return "/original/" + it.id;
    const st = await api(`/api/video/${it.id}/status`).catch(() => ({}));
    if (st.state === "done") return "/video/" + it.id;
    api(`/api/video/${it.id}/prepare`, {}).catch(() => {});
    return null;
  };
  const show = async k => {
    const my = ++seq;
    i = (k + ids.length) % ids.length;
    const id = ids[i];
    clearTimeout(timer);
    const it = await itemInfo(id);
    const vsrc = it && it.kind === "video" ? await videoSrc(it) : null;
    const url = "/screen/" + id;
    if (!vsrc) await new Promise(r => { const im = new Image(); im.onload = im.onerror = r; im.src = url; });
    if (!el.isConnected || my !== seq) return;
    stopVideo();
    const layer = layers[front = 1 - front];
    const r = () => (Math.random() * 6 - 3).toFixed(2) + "%";
    const zoomIn = Math.random() < 0.5;
    layer.className = "sl" + (o.kb && !vsrc ? " kb" : "");
    layer.style.cssText = `--dur:${o.seconds + 1.5}s;--s0:${zoomIn ? 1 : 1.12};--s1:${zoomIn ? 1.12 : 1};--x0:${r()};--y0:${r()};--x1:${r()};--y1:${r()}`;
    layer.innerHTML = `<div class="bg" style="background-image:url('${url}')"></div>` +
      (vsrc ? `<video playsinline poster="${url}" src="${vsrc}"></video>` : `<img src="${url}" alt="">`);
    void layer.offsetWidth;
    layer.classList.add("on");
    layers[1 - front].classList.remove("on");
    $(".pos", el).textContent = `${i + 1} / ${ids.length}`;
    const cap = it ? [fmtDate(it.taken, false), it.place].filter(Boolean).join(" · ") : "";
    $(".cap", el).textContent = cap + (it && it.kind === "video" && !vsrc ? " · Video wird noch vorbereitet – kommt beim nächsten Durchlauf" : "");
    preload(i + 1);
    preload(i + 2);
    if (vsrc) {
      video = $("video", layer);
      rotateVideo(video, it.userrot);
      musicPause();
      video.onended = () => { if (!paused) show(i + 1); };
      video.onerror = () => { stopVideo(); if (!paused) timer = setTimeout(() => show(i + 1), o.seconds * 1000); };
      if (!paused) video.play().catch(() => { video.muted = true; video.play().catch(() => {}); });
      return;
    }
    if (!paused) timer = setTimeout(() => show(i + 1), o.seconds * 1000);
  };
  const close = () => {
    clearTimeout(timer);
    seq++;
    if (video) { video.pause(); video = null; }
    let v = audio.volume;
    const fade = setInterval(() => { v -= 0.1; if (v <= 0) { clearInterval(fade); audio.pause(); } else audio.volume = v; }, 60);
    document.removeEventListener("keydown", keys, true);
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    el.remove();
  };
  const toggle = () => {
    paused = !paused;
    $("[data-k=pause]", el).textContent = paused ? "▶" : "❚❚";
    if (paused) { clearTimeout(timer); audio.pause(); if (video) video.pause(); }
    else if (video && !video.ended) video.play().catch(() => {});   // Video an derselben Stelle fortsetzen
    else { audio.play().catch(() => {}); show(i + 1); }
  };
  const keys = e => {
    if (e.key === "Escape") close();
    else if (e.key === "ArrowRight") show(i + 1);
    else if (e.key === "ArrowLeft") show(i - 1);
    else if (e.key === " ") toggle();
    else return;
    e.preventDefault(); e.stopPropagation();
  };
  document.addEventListener("keydown", keys, true);
  el.addEventListener("mousemove", () => { el.classList.add("ui"); clearTimeout(uiTimer); uiTimer = setTimeout(() => el.classList.remove("ui"), 2500); });
  el.addEventListener("click", e => {
    const k = e.target.closest("[data-k]");
    if (!k) return;
    ({ prev: () => show(i - 1), next: () => show(i + 1), pause: toggle, close })[k.dataset.k]();
  });
  if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
  document.addEventListener("fullscreenchange", function fc() { if (!document.fullscreenElement && el.isConnected) { /* Vollbild verlassen: weiter im Fenster */ } });
  show(0);
}

// --------------------------------------------------------------- Teilen ----
async function shareDialog(ids, name) {
  const st = await api("/api/share/status");
  const mac = st.platform === "darwin";
  const p = modal(`<div class="dlg"><b>${ids.length.toLocaleString("de-DE")} Fotos teilen</b>
    <div class="targets"><button id="tph"><span>📱</span>Aufs Handy</button><button id="tfr"><span>🖼</span>Frame-Kunstmodus</button>
      <button id="ttv"><span>📺</span>Auf dem Fernseher</button></div>
    <b style="display:block;margin-top:10px">… oder als Dateien exportieren</b>
    <div class="form">
      <label>Format</label><select id="xf">
        <option value="small">JPEG klein (1600 px) – WhatsApp, Mail</option>
        <option value="large">JPEG groß (2560 px) – Fotobuch, Bildschirm</option>
        <option value="original">Originaldateien (HEIC/RAW bleiben)</option></select>
      <label>Name</label><input type="text" id="xn" value="${esc(name || "")}" placeholder="z. B. Portugal 2018">
      <label>Als ZIP</label><label style="color:var(--text)"><input type="checkbox" id="xz"> eine ZIP-Datei (zum Mailen/Hochladen)</label>
      ${mac ? `<label>Apple Fotos</label><label style="color:var(--text)"><input type="checkbox" id="xa"> als Album in die Fotos-App (dort „Teilen › Geteiltes Album“)</label>` : ""}
    </div>
    <div class="help">Ziel: <b>${esc(st.dest)}</b>. Der Ordner öffnet sich danach – von dort per Ziehen in ein geteiltes iCloud-Album
    (icloud.com/photos oder Fotos-App), WhatsApp, Mail, Google Drive … Videos werden unverändert mitkopiert.</div>
    <div class="row"><button class="primary" id="xgo">Exportieren</button><button id="xno">Abbrechen</button></div></div>`);
  $("#xno", p).onclick = closePopover;
  $("#tph", p).onclick = () => phoneDialog(ids);
  $("#tfr", p).onclick = () => frameDialog(ids);
  $("#ttv", p).onclick = () => tvDialog(ids, name || `${ids.length} Fotos`);
  $("#xgo", p).onclick = async () => {
    const body = { ids, size: $("#xf", p).value, zip: $("#xz", p).checked, name: $("#xn", p).value.trim(), apple: mac && $("#xa", p).checked };
    closePopover();
    const r = await api("/api/export", body);
    if (r.error) return toast(r.error);
    toast("Export läuft …", 3000);
    if (selGrid) selGrid.clearSel();
    pollShare();
  };
}
// Mitlauf-Modus: aktuelles Foto des Betrachters an den Fernseher (kurz entprellt beim schnellen Blättern)
let tvSendTimer = null;
function sendTvCurrent() {
  clearTimeout(tvSendTimer);
  tvSendTimer = setTimeout(async () => {
    const id = viewer.ids[viewer.i];
    const prefetch = [viewer.ids[viewer.i + 1], viewer.ids[viewer.i - 1], viewer.ids[viewer.i + 2]].filter(Boolean);
    try { await api("/api/tv/one", { id, prefetch }); pollTv(); }
    catch (e) { toast("Fernseher: " + e.message, 5000); }
  }, 250);
}
function stopTvFollow(stopTv) {
  viewer.tvFollow = false;
  $(".v-tv").classList.remove("on");
  $(".v-tv").title = "Auf dem Fernseher / Frame zeigen";
  api("/api/tv/follow_end", { stop: stopTv }).then(() => pollTv());
  if (stopTv) toast("Fernseher beendet");
}

// ------------------------------------------------- Handy, Frame, Fernseher ----
async function phoneDialog(ids) {
  closePopover();
  let r;
  try { r = await api("/api/phone", { ids }); } catch (e) { return toast(e.message, 6000); }
  modal(`<b>📱 ${r.count} Foto${r.count === 1 ? "" : "s"} aufs Handy</b>
    <div class="qr">${r.svg}</div>
    <div class="help" style="text-align:center">Mit der Kamera des Handys scannen (gleiches WLAN). Dann Foto antippen und gedrückt halten ›
    „Zu Fotos hinzufügen“ bzw. „Bild herunterladen“. Gültig ${r.minutes} Minuten.</div>
    <div class="url">${esc(r.url)}</div>
    <div class="help">Fragt Windows nach der Firewall: „Zugriff zulassen“ für <b>private Netzwerke</b> wählen – sonst erreicht das Handy den Computer nicht.</div>`);
}

async function frameDialog(ids) {
  closePopover();
  const st = await api("/api/tv/status");
  if (!st.frame.ip) {
    const p = modal(`<b>🖼 Frame-Kunstmodus</b><p class="muted">Es ist noch kein Samsung The Frame eingerichtet.</p>
      <div class="row"><button class="primary" id="fset">Jetzt einrichten</button></div>`);
    $("#fset", p).onclick = () => { closePopover(); location.hash = "#/einstellungen"; };
    return;
  }
  const p = modal(`<div class="dlg"><b>🖼 ${ids.length} Foto${ids.length === 1 ? "" : "s"} in den Kunstmodus von „${esc(st.frame.name || st.frame.ip)}“</b>
    <div class="form"><label>Anzeige</label><select id="fss">
      <option value="0">${ids.length > 1 ? "erstes Foto zeigen, Rest in „Meine Fotos“" : "sofort zeigen"}</option>
      ${ids.length > 1 ? `<option value="3">als Frame-Diashow, alle 3 Minuten</option><option value="15">als Frame-Diashow, alle 15 Minuten</option>
      <option value="60">als Frame-Diashow, jede Stunde</option><option value="1440">als Frame-Diashow, täglich</option>` : ""}</select></div>
    <div class="help">${st.frame.paired ? "" : "Beim ersten Mal fragt der Fernseher, ob „FotoArchiv“ verbinden darf – bitte „Zulassen“ drücken. "}
    Die Fotos werden als 4K-Bild aufbereitet (Hochkant mit unscharfem Rand). FotoArchiv behält höchstens 60 eigene Bilder auf dem Frame.</div>
    <div class="row"><button class="primary" id="fgo">Senden</button><button id="fno">Abbrechen</button></div></div>`);
  $("#fno", p).onclick = closePopover;
  $("#fgo", p).onclick = async () => {
    const slideshow = +$("#fss", p).value;
    closePopover();
    const r = await api("/api/frame/send", { ids, slideshow });
    if (r.error) return toast(r.error, 5000);
    toast(st.frame.paired ? "Wird an den Frame gesendet …" : "Verbinde – bitte am Fernseher „Zulassen“ drücken …", 6000);
    if (selGrid) selGrid.clearSel();
    pollFrame();
  };
}
let frameTimer = null;
async function pollFrame() {
  clearTimeout(frameTimer);
  const s = await api("/api/tv/status");
  const j = s.frame_job;
  if (j.running) {
    const el = $("#indexstatus");
    el.hidden = false;
    el.innerHTML = `Frame: ${j.done}/${j.total}<div class="bar"><i style="width:${j.total ? Math.round(100 * j.done / j.total) : 0}%"></i></div>`;
    frameTimer = setTimeout(pollFrame, 1500);
  } else {
    toast(j.message || "Fertig", 7000);
    pollStatus();
  }
}

async function tvDialog(ids, title) {
  closePopover();
  const st = await api("/api/tv/status");
  if (!st.tv.name) {
    const p = modal(`<b>📺 Auf dem Fernseher</b><p class="muted">Es ist noch kein Fernseher eingerichtet.</p>
      <div class="row"><button class="primary" id="tset">Jetzt einrichten</button></div>`);
    $("#tset", p).onclick = () => { closePopover(); location.hash = "#/einstellungen"; };
    return;
  }
  const hasVideo = currentGrid && ids.some(id => currentGrid.videos.has(currentGrid.ids.indexOf(id)));
  const m = await api("/api/music");
  const p = modal(`<div class="dlg"><b>📺 Auf „${esc(st.tv.name)}“ abspielen</b>
    <div class="form">
      <label>Art</label><select id="tm"><option value="direct" selected>Sofort – Foto für Foto (startet direkt)</option>
        <option value="seamless">Mit Überblendung und Musik – wird vorher als Video berechnet (dauert, nur für kleine Auswahl)</option></select>
      <label>Dauer</label><select id="td"><option value="5">5 Sekunden</option><option value="8" selected>8 Sekunden</option>
        <option value="15">15 Sekunden</option><option value="30">30 Sekunden</option></select>
      <label class="tsm">Effekt</label><select id="te" class="tsm"><option value="fade">Überblenden (schnell vorbereitet)</option>
        <option value="kb">Kamerafahrt (Vorbereitung dauert länger)</option></select>
      <label class="tsm">Musik</label><select id="tmu" class="tsm"><option value="">ohne Musik (Videos mit Originalton)</option>
        ${m.music.length ? `<option value="__all">alle Titel (${m.music.length})</option>` : ""}
        ${m.music.map((x, i) => `<option value="${i}">${esc(x.name)}</option>`).join("")}</select>
      <label>Reihenfolge</label><select id="to"><option value="">wie angezeigt</option><option value="shuffle">zufällig</option></select>
      <label>Videos</label><label style="color:var(--text)"><input type="checkbox" id="tvv" ${hasVideo ? "checked" : ""}> Videos mit abspielen</label>
      <label class="tdi" hidden>Wiederholen</label><label class="tdi" style="color:var(--text)" hidden><input type="checkbox" id="tl" checked> in Endlosschleife</label></div>
    <div class="help">Der Fernseher wechselt dafür in den normalen Bildschirm (beim Frame: Kunstmodus aus). Steuerung unten rechts.
    ${m.music.length ? "" : "Musik: MP3/M4A in den Ordner FotoArchiv › Musik legen."}</div>
    <div class="row"><button class="primary" id="tgo">▶ Starten</button><button id="tno">Abbrechen</button></div></div>`);
  const sync = () => {
    const direct = $("#tm", p).value === "direct";
    $$(".tsm", p).forEach(x => (x.hidden = direct));
    $$(".tdi", p).forEach(x => (x.hidden = !direct));
  };
  $("#tm", p).onchange = sync;
  sync();
  $("#tno", p).onclick = closePopover;
  $("#tgo", p).onclick = () => {
    const mv = $("#tmu", p).value;
    const tracks = mv === "__all" ? m.music : mv !== "" ? [m.music[+mv]] : [];
    const o = { ids, seconds: +$("#td", p).value, shuffle: $("#to", p).value === "shuffle", videos: $("#tvv", p).checked,
      loop: $("#tl", p).checked, title, mode: $("#tm", p).value, kenburns: $("#te", p).value === "kb", music: tracks.map(t => t.path) };
    closePopover();
    startTvShow(o);
  };
}
async function startTvShow(o) {
  const st = await api("/api/tv/status");
  if (!st.tv.name) return tvDialog(o.ids, o.title);
  try {
    await api("/api/tv/show", o);
    toast("Läuft auf „" + st.tv.name + "“", 4000);
    if (selGrid) selGrid.clearSel();
    pollTv();
  } catch (e) { toast(e.message, 6000); }
}
let tvTimer = null;
// Pfeiltasten steuern die Fernseher-Diashow, solange sie läuft (nicht im Betrachter/Eingabefeldern)
document.addEventListener("keydown", e => {
  if ($("#tvbar").hidden || !viewer.el.hidden || $(".modal-bg") || e.target.matches("input, textarea, select")) return;
  const cmd = { ArrowRight: "next", ArrowLeft: "prev" }[e.key];
  if (!cmd) return;
  e.preventDefault();
  api("/api/tv/control", { cmd }).then(() => setTimeout(pollTv, 400));
});
async function pollTv() {
  clearTimeout(tvTimer);
  let s;
  try { s = (await api("/api/tv/status")).tv.show; } catch { return; }
  const bar = $("#tvbar");
  bar.hidden = !s.running;
  if (s.running) {
    const preparing = s.message && s.message.startsWith("wird vorbereitet");
    if (s.follow) {
      bar.innerHTML = `📺 <b>${esc(s.title)}</b> <span class="muted">läuft mit dem Betrachter mit</span>
        <button data-tv="stop" title="Fernseher beenden">⏹</button>${s.message ? `<span class="muted">⚠ ${esc(s.message.slice(0, 60))}</span>` : ""}`;
      $$("[data-tv]", bar).forEach(b => b.onclick = async () => { await api("/api/tv/control", { cmd: b.dataset.tv });
        viewer.tvFollow = false; $(".v-tv").classList.remove("on"); setTimeout(pollTv, 400); });
      tvTimer = setTimeout(pollTv, 4000);
      pollTv.was = s.running;
      return;
    }
    bar.innerHTML = `📺 <b>${esc(s.title)}</b> <span class="muted">${preparing ? esc(s.message) : `${s.i}/${s.total}`}</span>
      ${preparing ? "" : `<button data-tv="prev" title="Voriges Foto (←)">‹</button><button data-tv="pause" title="${s.paused ? "Weiter automatisch" : "Anhalten – dann mit ‹ › manuell blättern"}">${s.paused ? "▶" : "❚❚"}</button>
      <button data-tv="next" title="Nächstes Foto (→)">›</button>`}
      <button data-tv="stop" title="Beenden">⏹</button>${s.message && !preparing ? `<span class="muted" title="${esc(s.message)}">⚠ ${esc(s.message.slice(0, 60))}</span>` : ""}`;
    $$("[data-tv]", bar).forEach(b => b.onclick = async () => { await api("/api/tv/control", { cmd: b.dataset.tv }); setTimeout(pollTv, 400); });
    tvTimer = setTimeout(pollTv, 2500);
  } else if (s.message && pollTv.was) {
    toast("Fernseher: " + s.message, 8000);
  }
  pollTv.was = s.running;
}

async function bindTvSettings(page) {
  const box = $("#tvset", page), found = $("#tvfound", page);
  const show = async () => {
    const s = await api("/api/tv/status");
    box.innerHTML = `Diashow/Videos: <b>${esc(s.tv.name || "nicht eingerichtet")}</b>${s.tv.ip ? ` (${esc(s.tv.ip)})` : ""} ·
      Kunstmodus: <b>${esc(s.frame.name || "nicht eingerichtet")}</b>${s.frame.ip ? ` (${esc(s.frame.ip)}, ${s.frame.paired ? "gekoppelt" : "noch nicht gekoppelt"})` : ""}
      ${s.frame.ip ? ` <button class="ghost" id="fpair">Verbindung testen</button>` : ""}${s.frame.uploaded ? ` <button class="ghost" id="fclean">${s.frame.uploaded} FotoArchiv-Bilder vom Frame löschen</button>` : ""}`;
    const fp = $("#fpair", box), fc = $("#fclean", box);
    if (fp) fp.onclick = async () => {
      toast("Verbinde … ggf. am Fernseher „Zulassen“ drücken", 8000);
      const r = await api("/api/frame/pair", {});
      toast(r.ok ? "Frame verbunden – Kunstmodus bereit" : (r.error || "Kein Kunstmodus gefunden"), 6000);
      show();
    };
    if (fc) fc.onclick = async () => {
      if (!await confirmBox("Alle von FotoArchiv gesendeten Bilder vom Frame löschen?")) return;
      try { const r = await api("/api/frame/cleanup", {}); toast(`${r.deleted} Bilder gelöscht`); } catch (e) { toast(e.message, 6000); }
      show();
    };
  };
  const list = (renderers, frames) => {
    found.innerHTML = (renderers.map((d, i) => `<div class="acc"><b>📺 ${esc(d.name)}</b><span class="muted">${esc([d.maker, d.model, d.ip].filter(Boolean).join(" · "))}</span>
        <span style="flex:1"></span><button data-r="${i}">Für Diashow/Videos verwenden</button></div>`).join("") +
      frames.filter(f => f.frame).map((f, i) => `<div class="acc"><b>🖼 ${esc(f.name)}</b><span class="muted">${esc([f.model, f.ip].join(" · "))} · The Frame</span>
        <span style="flex:1"></span><button data-f="${i}">Für Kunstmodus verwenden</button></div>`).join(""))
      || `<p class="muted">Kein Fernseher gefunden. Ist er eingeschaltet und im selben WLAN? Sonst IP-Adresse eintragen (steht in den Netzwerkeinstellungen des Fernsehers oder in der Fritzbox).</p>`;
    $$("[data-r]", found).forEach(b => b.onclick = async () => { await api("/api/tv/select", { renderer: renderers[+b.dataset.r] }); toast("Gespeichert"); show(); });
    const fr = frames.filter(f => f.frame);
    $$("[data-f]", found).forEach(b => b.onclick = async () => { await api("/api/tv/select", { frame: fr[+b.dataset.f] }); toast("Gespeichert"); show(); });
  };
  const dmsBox = $("#dmson", page);
  if (dmsBox) {
    const upd = s => { dmsBox.checked = s.enabled; const tv = Object.keys(s.seen || {}).filter(ip => !ip.startsWith("127.") && ip !== location.hostname);
      $("#dmsst", page).textContent = s.enabled ? (s.running ? "– aktiv" + (tv.length ? ", Fernseher verbunden" : "") : "– startet …") : ""; };
    api("/api/dms").then(upd);
    dmsBox.onchange = async () => {
      upd(await api("/api/dms", { enabled: dmsBox.checked }));
      if (dmsBox.checked) toast("Am Fernseher: Quelle › „FotoArchiv“ (kann eine Minute dauern, bis er erscheint)", 7000);
      setTimeout(async () => upd(await api("/api/dms")), 2000);
    };
  }
  $("#tvsearch", page).onclick = async () => {
    found.innerHTML = `<p class="muted">Suche im Heimnetz …</p>`;
    const r = await api("/api/tv/discover", {});
    list(r.renderers, r.frames);
  };
  $("#tvipok", page).onclick = async () => {
    const ip = $("#tvip", page).value.trim();
    if (!ip) return;
    try {
      const r = await api("/api/tv/manual", { ip });
      list(r.renderer ? [r.renderer] : [], r.frame ? [r.frame] : []);
    } catch (e) { toast(e.message, 6000); }
  };
  show();
}

let shareTimer = null;
async function pollShare() {
  clearTimeout(shareTimer);
  const s = await api("/api/share/status");
  const el = $("#indexstatus");
  if (s.running) {
    el.hidden = false;
    const pct = s.total ? Math.round(100 * s.done / s.total) : 0;
    el.innerHTML = `${esc(s.title)} ${pct}%<div class="bar"><i style="width:${pct}%"></i></div>`;
    shareTimer = setTimeout(pollShare, 1000);
  } else {
    toast(s.message || "Fertig", 8000);
    pollStatus();
  }
}

// --------------------------------------------------------------- Privat ----
function setPriv(p) {
  if (!p) return;
  const wasUnlocked = state.priv.unlocked;
  state.priv = p;
  const b = $("#lockbtn");
  b.hidden = !p.password;
  b.textContent = p.unlocked ? "🔓 Privat sichtbar" : "🔒 Privat gesperrt";
  b.title = p.unlocked ? "Klicken zum Sperren" : "Klicken zum Entsperren";
  b.classList.toggle("open", p.unlocked);
  // automatisch gesperrt (Zeitablauf): Ansicht neu laden, damit nichts Privates stehen bleibt
  if (wasUnlocked === true && !p.unlocked && p.password) location.reload();
}
$("#lockbtn").onclick = async () => {
  if (state.priv.unlocked) lockNow();
  else if (await unlockDialog()) route();
};
async function lockNow() {
  await api("/api/private/lock", {});
  location.reload();
}
function lockScreen(text) {
  clearMain();
  main.innerHTML = `<div class="lockscreen"><div class="big">🔒</div><p>${esc(text)}</p><button class="primary" id="lsu">Entsperren …</button></div>`;
  $("#lsu", main).onclick = async () => { if (await unlockDialog()) route(); };
}
function unlockDialog() {
  return new Promise(res => {
    const p = modal(`<b>🔒 Private Fotos entsperren</b>
      <div class="row"><input type="password" id="upw" placeholder="Passwort" style="width:100%" autocomplete="off"></div>
      <div class="row"><button class="primary" id="uok">Entsperren</button><button id="uno">Abbrechen</button></div>`);
    const inp = $("#upw", p);
    inp.focus();
    const ok = async () => {
      try {
        await api("/api/private/unlock", { password: inp.value });
        closePopover();
        setPriv({ password: true, unlocked: true });
        toast("Entsperrt – wird nach 30 Minuten ohne Nutzung wieder gesperrt", 4000);
        res(true);
      } catch (e) { inp.value = ""; inp.focus(); toast("Passwort falsch"); }
    };
    inp.addEventListener("keydown", e => { if (e.key === "Enter") ok(); if (e.key === "Escape") { closePopover(); res(false); } e.stopPropagation(); });
    $("#uok", p).onclick = ok;
    $("#uno", p).onclick = () => { closePopover(); res(false); };
  });
}
function passwordDialog(change) {
  return new Promise(res => {
    const p = modal(`<div class="dlg"><b>${change ? "Passwort für Privates ändern" : "Passwort für Privates festlegen"}</b>
      <div class="form">${change ? `<label>Bisheriges</label><input type="password" id="pwo" autocomplete="off">` : ""}
        <label>Neues Passwort</label><input type="password" id="pwn" autocomplete="new-password">
        <label>Wiederholen</label><input type="password" id="pwr" autocomplete="new-password"></div>
      <div class="help">Mindestens 4 Zeichen. Gilt für diese Festplatte – an jedem Computer dasselbe Passwort. Vergessen lässt es sich nicht wiederherstellen.</div>
      <div class="row"><button class="primary" id="pwok">Speichern</button><button id="pwno">Abbrechen</button></div></div>`);
    $("#" + (change ? "pwo" : "pwn"), p).focus();
    $("#pwno", p).onclick = () => { closePopover(); res(false); };
    $("#pwok", p).onclick = async () => {
      const n = $("#pwn", p).value, r = $("#pwr", p).value;
      if (n !== r) return toast("Die beiden Eingaben stimmen nicht überein");
      try {
        await api("/api/private/password", { new: n, old: change ? $("#pwo", p).value : undefined });
        closePopover();
        setPriv({ password: true, unlocked: true });
        toast("Passwort gespeichert");
        res(true);
      } catch (e) { toast(e.message, 5000); }
    };
  });
}
async function ensurePassword() {
  if (!state.priv.password) {
    toast("Zuerst ein Passwort für Privates festlegen", 4000);
    return passwordDialog(false);
  }
  return true;
}

// Gemeinsame Dialoge für Auswahl und Betrachter
async function actAlbum(ids) {
  const r = await pickAlbum(`${ids.length === 1 ? "Foto" : ids.length + " Fotos"} hinzufügen zu …`, { allowNew: true });
  if (!r) return null;
  if (r.new) { await api("/api/albums", { name: r.new, ids }); return r.new; }
  await api(`/api/albums/${r.id}/add`, { ids });
  return r.label;
}
async function actTag(ids) {
  const tags = await api("/api/tags");
  const t = await pickFrom("Stichwort hinzufügen", tags.map(([t, c]) => ({ id: t, label: t, note: c })), { allowNew: true, placeholder: "z. B. Strand, Geburtstag …" });
  if (!t) return null;
  const tag = t.new || t.id;
  await api("/api/items/tags", { ids, add: [tag] });
  return tag;
}
async function actPerson(ids) {
  const name = await askName("Wer ist auf dem Foto?", "", true);
  if (!name) return null;
  await api("/api/items/person", { ids, name });
  await loadPersons();
  return name;
}
async function actDate(ids, current) {
  const p = modal(`<b>Aufnahmedatum ${ids.length > 1 ? "für " + ids.length + " Fotos " : ""}setzen</b>
    <div class="row"><input type="date" id="dd" value="${(current || "").slice(0, 10)}"><input type="time" id="dt" value="${(current || "").slice(11, 16)}"></div>
    <div class="muted" style="font-size:12px;margin-top:6px">Ohne Uhrzeit bleibt die bisherige Uhrzeit jedes Fotos erhalten.</div>
    <div class="row"><button class="primary" id="dok">Speichern</button><button id="dno">Abbrechen</button></div>`);
  return new Promise(res => {
    $("#dno", p).onclick = () => { closePopover(); res(false); };
    $("#dok", p).onclick = async () => {
      const d = $("#dd", p).value, t = $("#dt", p).value;
      if (!d) return toast("Bitte Datum wählen");
      closePopover();
      await api("/api/items/date", { ids, date: t && ids.length === 1 ? `${d} ${t}` : d });
      res(true);
    };
  });
}
function askText(title, value = "") {
  return new Promise(res => {
    const p = modal(`<b>${esc(title)}</b>
      <div class="row"><input type="text" id="atx" style="width:100%"></div>
      <div class="row"><button class="primary" id="aok">OK</button><button id="ano">Abbrechen</button></div>`);
    const inp = $("#atx", p);
    inp.value = value;
    inp.focus();
    inp.select();
    const ok = () => { const v = inp.value.trim(); closePopover(); res(v || null); };
    inp.addEventListener("keydown", e => { if (e.key === "Enter") ok(); if (e.key === "Escape") { closePopover(); res(null); } e.stopPropagation(); });
    $("#aok", p).onclick = ok;
    $("#ano", p).onclick = () => { closePopover(); res(null); };
  });
}
function askRange(title, a, b) {
  return new Promise(res => {
    const p = modal(`<b>${esc(title)}</b>
      <div class="row"><label>von <input type="date" id="ra" value="${a}"></label><label>bis <input type="date" id="rb" value="${b}"></label></div>
      <div class="row"><button class="primary" id="rok">OK</button><button id="rno">Abbrechen</button></div>`);
    $("#rno", p).onclick = () => { closePopover(); res(null); };
    $("#rok", p).onclick = () => {
      const s = $("#ra", p).value, e = $("#rb", p).value || $("#ra", p).value;
      if (!s) return toast("Bitte Startdatum wählen");
      closePopover();
      res({ start: s, end: e });
    };
  });
}
// Auswahlliste mit Suchfeld und optional „Neu anlegen“
function pickFrom(title, items, opts = {}) {
  return new Promise(res => {
    const p = modal(`<div class="picker"><b>${esc(title)}</b>
      <div class="row"><input type="text" id="pk" placeholder="${esc(opts.placeholder || "Suchen …")}"></div><div class="list"></div></div>`);
    const inp = $("#pk", p), list = $(".list", p);
    let sel = 0, shown = [];
    const render = () => {
      const v = inp.value.trim().toLowerCase();
      shown = items.filter(x => !v || x.label.toLowerCase().includes(v)).slice(0, 80);
      if (opts.allowNew && v && !items.some(x => x.label.toLowerCase() === v)) shown.push({ new: inp.value.trim(), label: `Neu: „${inp.value.trim()}“` });
      sel = Math.min(sel, Math.max(0, shown.length - 1));
      list.innerHTML = shown.map((x, i) => `<div class="${i === sel ? "sel" : ""} ${x.new ? "new" : ""}" data-i="${i}">${esc(x.label)}${x.note != null ? `<small>${esc(x.note)}</small>` : ""}</div>`).join("")
        || `<span class="muted">${opts.allowNew ? "Namen eintippen" : "Nichts gefunden"}</span>`;
      $$("[data-i]", list).forEach(d => d.onclick = () => { closePopover(); res(shown[+d.dataset.i]); });
    };
    inp.addEventListener("input", () => { sel = 0; render(); });
    inp.addEventListener("keydown", e => {
      if (e.key === "ArrowDown") { sel = Math.min(shown.length - 1, sel + 1); render(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); render(); e.preventDefault(); }
      else if (e.key === "Enter" && shown[sel]) { closePopover(); res(shown[sel]); }
      else if (e.key === "Escape") { closePopover(); res(null); }
      e.stopPropagation();
    });
    render();
    inp.focus();
  });
}
async function pickAlbum(title, opts = {}) {
  const d = await api("/api/albums");
  const byId = Object.fromEntries(d.albums.map(a => [a.id, a]));
  const path = a => { const parts = []; for (let x = a, k = 0; x && k < 10; x = byId[x.parent], k++) parts.unshift(x.name); return parts.join(" › "); };
  let items = d.albums.filter(a => a.id !== opts.exclude).map(a => ({ id: a.id, label: path(a), note: a.count }))
    .sort((x, y) => x.label.localeCompare(y.label, "de"));
  if (opts.allowTop) items.unshift({ top: true, label: "(oberste Ebene)" });
  return pickFrom(title, items, { allowNew: opts.allowNew, placeholder: opts.allowNew ? "Album suchen oder neuen Namen eingeben" : "Album suchen" });
}

window.openExternal = id => api(`/api/item/${id}/open`, {});
window.revealExternal = id => api(`/api/item/${id}/reveal`, {});
window.closeViewer = closeViewer;

function step(d) {
  const n = viewer.i + d;
  if (n < 0 || n >= viewer.ids.length) return;
  viewer.i = n;
  closePopover();
  showItem();
}
$(".v-prev").onclick = () => step(-1);
$(".v-next").onclick = () => step(1);
$(".v-close").onclick = closeViewer;
$(".v-info").onclick = () => togglePanel();
$(".v-edit").onclick = () => openEditor();
$(".v-vadd").onclick = e => { if (viewer.item && !viewer.item.locked) quickAddVideo([viewer.item.id], e.shiftKey); };
$(".v-faces").onclick = () => toggleFaces();
$(".v-fav").onclick = () => toggleFav();
$(".v-del").onclick = () => viewerDelete();
$(".v-tv").onclick = () => {
  const it = viewer.item;
  if (viewer.tvFollow) { stopTvFollow(true); return; }
  if (!it || it.locked) return;
  const p = modal(`<b>Auf dem Fernseher zeigen</b><div class="targets" style="grid-template-columns:1fr 1fr">
    <button id="vt1"><span>📺</span>${it.kind === "video" ? "Video abspielen" : "Auf dem Fernseher"}</button>
    ${it.kind === "video" ? "" : `<button id="vt2"><span>🖼</span>Frame-Kunstmodus</button>`}</div>
    <div class="help">„Auf dem Fernseher“: Beim Blättern mit ← → läuft der Fernseher mit. Eine Diashow startest du über „▶ Diashow“.</div>`);
  $("#vt1", p).onclick = async () => {
    closePopover();
    const st = await api("/api/tv/status");
    if (!st.tv.name) return tvDialog([it.id], it.name);
    viewer.tvFollow = true;
    $(".v-tv").classList.add("on");
    $(".v-tv").title = "Fernseher läuft mit – klicken zum Beenden";
    toast("Fernseher läuft mit: mit ← → blättern, das Foto erscheint auch auf dem Fernseher", 5000);
    sendTvCurrent();
  };
  const v2 = $("#vt2", p);
  if (v2) v2.onclick = () => { closePopover(); frameDialog([it.id]); };
};
$(".v-rotl").onclick = () => rotate("ccw");
$(".v-rotr").onclick = () => rotate("cw");
async function rotate(dir) {
  const id = viewer.ids[viewer.i];
  if (!viewer.item) return;
  await api(`/api/item/${id}/rotate`, { dir });
  thumbVer[id] = Date.now();
  if (currentGrid) $$(`.cell img`, currentGrid.el).forEach(img => {
    if (+img.closest(".cell").dataset.i === currentGrid.ids.indexOf(id)) img.src = thumbUrl(id);
  });
  showItem();
}
// Video von Hand gedreht (falsch aufgenommen): im Browser per CSS drehen, Datei bleibt unverändert
function rotateVideo(v, rot) {
  rot = rot || 0;
  if (!v || !rot) return;
  const fit = () => {
    const box = v.parentElement.getBoundingClientRect();
    const side = rot % 180 !== 0;
    v.style.maxWidth = (side ? box.height : box.width) + "px";
    v.style.maxHeight = (side ? box.width : box.height) + "px";
    v.style.transform = `rotate(${rot}deg)`;
  };
  fit();
  v.addEventListener("loadedmetadata", fit);
  window.addEventListener("resize", () => v.isConnected && fit());
}
function togglePanel() {
  viewer.panel = !viewer.panel;
  localStorageSet("panel", viewer.panel ? "1" : "0");
  viewer.el.classList.toggle("panel-open", viewer.panel);
  $(".v-panel").hidden = !viewer.panel;
  renderPanel();
}
function toggleFaces() {
  viewer.showFaces = !viewer.showFaces;
  localStorageSet("faces", viewer.showFaces ? "1" : "0");
  $(".v-faces").classList.toggle("on", viewer.showFaces);
  drawFaces();
}
async function toggleFav() {
  const it = viewer.item;
  if (!it) return;
  it.fav = it.fav ? 0 : 1;
  await api(`/api/item/${it.id}/fav`, { fav: it.fav });
  $(".v-fav").textContent = it.fav ? "★" : "☆";
  $(".v-fav").classList.toggle("on", !!it.fav);
}
document.addEventListener("keydown", e => {
  if (viewer.el.hidden || e.target.matches("input, textarea") || (typeof ed !== "undefined" && ed.open)) return;
  if (e.key === "ArrowRight") step(1);
  else if (e.key === "ArrowLeft") step(-1);
  else if (e.key === "Escape") { if ($(".popover")) closePopover(); else closeViewer(); }
  else if (e.key === "i" || e.key === "I") togglePanel();
  else if (e.key === "g" || e.key === "G") toggleFaces();
  else if (e.key === "f" || e.key === "F") toggleFav();
  else if (e.key === "r" || e.key === "R") rotate("cw");
  else if (e.key === "e" || e.key === "E") openEditor();
  else if (e.key === "Delete") viewerDelete();
  else return;
  e.preventDefault();
});
// Wischen auf Touch-Geräten
let touchX = null;
viewer.el.addEventListener("touchstart", e => { touchX = e.touches[0].clientX; }, { passive: true });
viewer.el.addEventListener("touchend", e => {
  if (touchX === null) return;
  const dx = e.changedTouches[0].clientX - touchX;
  if (Math.abs(dx) > 60) step(dx < 0 ? 1 : -1);
  touchX = null;
});

// ----------------------------------------------------------------- Suche ----
const search = $("#search"), sugg = $("#suggest");
let suggSel = -1, suggList = [];
function renderChips() {
  $("#chips").innerHTML = state.chips.map(id => {
    const p = state.persons.find(x => x.id === id);
    return `<span class="chip">${p && p.cover ? `<img src="/face/${p.cover}">` : ""}${esc(p ? p.name : "?")}<button data-id="${id}" title="Entfernen">✕</button></span>`;
  }).join("");
  $$("#chips button").forEach(b => b.onclick = () => { state.chips = state.chips.filter(x => x !== +b.dataset.id); renderChips(); runSearch(); });
}
function addChip(id) {
  if (!state.chips.includes(id)) state.chips.push(id);
  renderChips();
}
function updateSuggest() {
  const v = search.value.trim().toLowerCase();
  const last = v.split(/\s+/).pop();
  suggList = last ? state.persons.filter(p => !p.hidden && p.name.toLowerCase().startsWith(last) && !state.chips.includes(p.id)).slice(0, 6) : [];
  sugg.innerHTML = suggList.map((p, i) => `<div class="${i === suggSel ? "sel" : ""}" data-i="${i}">${p.cover ? `<img src="/face/${p.cover}">` : ""}
    <span>Person: <b>${esc(p.name)}</b></span><small>${p.items} Fotos</small></div>`).join("");
  sugg.hidden = !suggList.length || document.activeElement !== search;
  $$("div", sugg).forEach(d => d.onmousedown = e => { e.preventDefault(); pickSuggest(+d.dataset.i); });
}
function pickSuggest(i) {
  const p = suggList[i];
  if (!p) return;
  const words = search.value.trim().split(/\s+/);
  words.pop();
  search.value = words.join(" ");
  addChip(p.id);
  suggSel = -1;
  sugg.hidden = true;
  runSearch();
}
let searchTimer;
search.addEventListener("input", () => {
  suggSel = -1;
  updateSuggest();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 450);
});
search.addEventListener("keydown", e => {
  if (e.key === "ArrowDown" && suggList.length) { suggSel = Math.min(suggList.length - 1, suggSel + 1); updateSuggest(); e.preventDefault(); }
  else if (e.key === "ArrowUp" && suggList.length) { suggSel = Math.max(-1, suggSel - 1); updateSuggest(); e.preventDefault(); }
  else if (e.key === "Enter") {
    if (suggSel >= 0) pickSuggest(suggSel);
    else { clearTimeout(searchTimer); sugg.hidden = true; runSearch(); }
  } else if (e.key === "Backspace" && !search.value && state.chips.length) {
    state.chips.pop(); renderChips(); runSearch();
  } else if (e.key === "Escape") { sugg.hidden = true; }
});
search.addEventListener("blur", () => setTimeout(() => (sugg.hidden = true), 150));
search.addEventListener("focus", updateSuggest);

function runSearch() {
  state.filters.q = search.value.trim();
  const page = location.hash.split("?")[0];
  if (page === "#/fotos") routes.fotos();
  else if (page === "#/karte") routes.karte();
  else if (page === "#/favoriten") routes.favoriten();
  else location.hash = "#/fotos";
}

// Filterleiste
$("#filterbtn").onclick = () => {
  const f = $("#filters");
  f.hidden = !f.hidden;
  document.body.classList.toggle("filters-open", !f.hidden);
};
const bindFilter = (id, key, prop = "value") => $(id).addEventListener("change", e => { state.filters[key] = e.target[prop]; markFilterBtn(); runSearch(); });
bindFilter("#f-from", "from");
bindFilter("#f-to", "to");
bindFilter("#f-kind", "kind");
bindFilter("#f-fav", "fav", "checked");
bindFilter("#f-nodate", "nodate", "checked");
bindFilter("#f-dups", "dups", "checked");
bindFilter("#f-sort", "sort");
$("#f-reset").onclick = () => {
  Object.assign(state.filters, { from: "", to: "", kind: "", fav: false, nodate: false, dups: false, sort: "desc" });
  ["#f-from", "#f-to", "#f-kind"].forEach(s => ($(s).value = ""));
  ["#f-fav", "#f-nodate", "#f-dups"].forEach(s => ($(s).checked = false));
  $("#f-sort").value = "desc";
  markFilterBtn();
  runSearch();
};
// Umschalter Alle | Fotos | Videos: lädt die aktuelle Ansicht (Album, Ordner …) neu, statt zur Zeitleiste zu springen
$$("#kindsw [data-kind]").forEach(b => b.onclick = () => {
  state.filters.kind = b.dataset.kind;
  $("#f-kind").value = b.dataset.kind;
  markFilterBtn();
  route();
});
function markFilterBtn() {
  const f = state.filters;
  $$("#kindsw [data-kind]").forEach(b => b.classList.toggle("on", b.dataset.kind === (f.kind === "raw" ? "photo" : f.kind || "")));
  const n = [f.from, f.to, f.kind, f.fav, f.nodate, f.dups, f.sort !== "desc"].filter(Boolean).length;
  $("#filterbtn").textContent = n ? `Filter (${n})` : "Filter";
  $("#filterbtn").classList.toggle("primary", !!n);
}

// ------------------------------------------------------------ Seitenleiste ----
async function loadYears() {
  const years = await api("/api/years");
  $("#years").innerHTML = years.map(([y, n]) => `<a href="#/fotos?jahr=${y}" data-y="${y}">${y}<small>${n.toLocaleString("de-DE")}</small></a>`).join("");
  $$("#years a").forEach(a => a.onclick = e => {
    if (currentGrid && location.hash.startsWith("#/fotos")) {
      e.preventDefault();
      currentGrid.scrollToGroupPrefix(a.dataset.y);
    }
  });
}

// ------------------------------------------------------------ Indexstatus ----
let pollTimer = null;
async function pollStatus(force) {
  clearTimeout(pollTimer);
  let s;
  try { s = await api("/api/status"); } catch { pollTimer = setTimeout(pollStatus, 10000); return; }
  const el = $("#indexstatus"), pr = s.progress;
  setPriv(s.private);
  if (pr.running || s.recompute) {
    const pct = pr.total ? Math.round(100 * pr.done / pr.total) : 0;
    el.hidden = false;
    el.innerHTML = s.recompute && !pr.running ? "Personenerkennung läuft …"
      : `${esc(pr.phase)} ${pr.total ? pct + "%" : ""}${pr.eta ? " · " + fmtEta(pr.eta) : ""}<div class="bar"><i style="width:${pct}%"></i></div>`;
    pollStatus.wasRunning = true;
    pollTimer = setTimeout(pollStatus, 3000);
  } else {
    el.hidden = true;
    if (pollStatus.wasRunning) {
      pollStatus.wasRunning = false;
      toast("Bibliothek ist aktuell");
      loadYears();
      loadPersons();
    }
    if (force) pollTimer = setTimeout(pollStatus, 2000);
    else pollTimer = setTimeout(pollStatus, 30000);
  }
  return s;
}
$("#indexstatus").onclick = () => (location.hash = "#/einstellungen");

// ---------------------------------------------------------------- Karte ----
function loadLeaflet() {
  if (window.L && L.markerClusterGroup) return Promise.resolve();
  const css = href => new Promise((ok, bad) => { const l = document.createElement("link"); l.rel = "stylesheet"; l.href = href; l.onload = ok; l.onerror = bad; document.head.appendChild(l); });
  const js = src => new Promise((ok, bad) => { const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = bad; document.head.appendChild(s); });
  const base = "https://unpkg.com/";
  return Promise.all([css(base + "leaflet@1.9.4/dist/leaflet.css"), css(base + "leaflet.markercluster@1.5.3/dist/MarkerCluster.css"),
    css(base + "leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css")])
    .then(() => js(base + "leaflet@1.9.4/dist/leaflet.js"))
    .then(() => js(base + "leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"));
}

// ---------------------------------------------------------------- Router ----
async function route() {
  closePopover();
  if (!viewer.el.hidden) closeViewer();
  const h = location.hash.replace(/^#\/?/, "").split("?")[0];
  const [name, ...rest] = h.split("/").map(decodeURIComponent);
  const page = name || "start";
  $$("#side > a").forEach(a => a.classList.toggle("active", a.dataset.nav === page ||
    (a.dataset.nav === "personen" && (page === "person" || page === "unbekannt")) ||
    (a.dataset.nav === "alben" && (page === "album" || page === "ereignis" || page === "ausgeblendet"))));
  document.body.classList.toggle("trashview", page === "papierkorb");
  const fn = routes[page] || routes.fotos;
  if (page !== "start") await personsReady;  // andere Seiten brauchen die Personenliste
  try {
    await fn(...(page === "ordner" ? [rest.join("/")] : rest));
  } catch (e) {
    console.error(e);
    main.innerHTML = `<div class="empty">Fehler: ${esc(e.message)}</div>`;
  }
}
window.addEventListener("hashchange", route);

// Sofort die Übersicht zeigen; Personen, Jahre und Status laden nebenher (die USB-Platte kann träge sein)
const personsReady = loadPersons().catch(() => []);
(async function init() {
  const fresh = !location.hash || location.hash === "#/";
  if (fresh) history.replaceState(null, "", "#/start");
  loadYears().catch(() => {});
  route();
  const s = await pollStatus();
  pollTv();
  if (s && !s.items && fresh) location.hash = "#/einstellungen";
})();
