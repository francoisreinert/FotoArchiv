// FotoArchiv – Zweisprachigkeit (Deutsch/Englisch). Die Quelltexte bleiben deutsch; bei Englisch übersetzt diese Datei
// jeden Text beim Einfügen ins DOM (Wörterbuch window.I18N_EN aus i18n_en.js): exakt → Muster mit {1} → Teilstücke.
// Sprache: localStorage „fa_lang“, sonst Browsersprache (de… = Deutsch, alles andere Englisch).
(function () {
  let lang = null;
  try { lang = localStorage.getItem("fa_lang"); } catch { /* egal */ }
  const q = /[?&]lang=(de|en)\b/.exec(location.search);  // z. B. http://127.0.0.1:8765/?lang=en
  if (q) { lang = q[1]; try { localStorage.setItem("fa_lang", lang); } catch { /* egal */ } }
  if (lang !== "de" && lang !== "en") lang = /^de\b/i.test(navigator.language || "de") ? "de" : "en";
  window.LANG = lang;
  window.setLang = l => { try { localStorage.setItem("fa_lang", l); } catch { /* egal */ } location.reload(); };
  window.tr = s => s;
  if (lang !== "en") return;
  document.documentElement.lang = "en";

  const D = window.I18N_EN || {};
  const exact = new Map(), pats = [], frags = [];
  const reEsc = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  for (const [de, en] of Object.entries(D)) {
    if (!en || de === en) continue;
    if (/\{\d+\}/.test(de)) {
      const lits = de.split(/\{\d+\}/);
      const lit = lits.reduce((a, b) => (b.trim().length > a.length ? b.trim() : a), "");
      if (!/[A-Za-zÄÖÜäöüß]{2}/.test(lit)) continue;
      const order = [...de.matchAll(/\{(\d+)\}/g)].map(m => +m[1]);
      pats.push({ re: new RegExp("^" + lits.map(reEsc).join("(.+?)") + "$"), lit, order, en, len: lits.join("").length });
    } else {
      exact.set(de, en);
      if (de.length >= 10 && /\s/.test(de.trim())) frags.push([de, en]);
    }
  }
  pats.sort((a, b) => b.len - a.len);
  frags.sort((a, b) => b[0].length - a[0].length);
  const MONTHS = { Januar: "January", Februar: "February", März: "March", Mai: "May", Juni: "June", Juli: "July",
    Oktober: "October", Dezember: "December", Mär: "Mar", Okt: "Oct", Dez: "Dec" };
  const monRe = new RegExp("(^|[^\\wÄÖÜäöüß])(" + Object.keys(MONTHS).join("|") + ")(?=$|[^\\wÄÖÜäöüß])", "g");

  const cache = new Map();
  function core(k, depth) {
    let r = exact.get(k);
    if (r != null) return r;
    for (const p of pats) {
      if (!k.includes(p.lit)) continue;
      const m = p.re.exec(k);
      if (!m) continue;
      const val = {};
      p.order.forEach((n, i) => { const v = m[i + 1]; val[n] = depth < 2 ? (core(v.trim(), depth + 1) ?? v) : v; });
      return p.en.replace(/\{(\d+)\}/g, (_, n) => val[n] ?? "");
    }
    let out = k, hit = false;
    for (const [de, en] of frags) if (out.includes(de)) { out = out.split(de).join(en); hit = true; }
    const mo = out.replace(monRe, (_, a, w) => a + MONTHS[w]);
    if (mo !== out) { out = mo; hit = true; }
    return hit ? out : null;
  }
  function tx(s) {
    if (!s || s.length > 3000 || !/[A-Za-zÄÖÜäöüß]{2}/.test(s)) return null;
    const k = s.replace(/\s+/g, " ").trim();
    let r = cache.get(k);
    if (r === undefined) {
      r = core(k, 0);
      if (cache.size > 20000) cache.clear();
      cache.set(k, r);
    }
    if (r == null || r === k) return null;
    return (/^\s/.test(s) ? " " : "") + r + (/\s$/.test(s) ? " " : "");
  }
  window.tr = s => tx(String(s)) ?? s;

  const NO = 'script,style,textarea,code,[translate="no"],[contenteditable="true"]';
  const ATTRS = ["title", "placeholder", "aria-label", "alt"];
  const mine = new WeakMap();  // Knoten → zuletzt von uns gesetzter Text (keine Schleifen)
  function doText(n) {
    const v = n.nodeValue;
    if (mine.get(n) === v) return;
    const p = n.parentElement;
    if (!p || p.closest(NO)) return;
    const r = tx(v);
    if (r != null) { mine.set(n, r); n.nodeValue = r; }
  }
  function doAttr(el, a) {
    const v = el.getAttribute(a);
    if (!v) return;
    const r = tx(v);
    if (r != null) el.setAttribute(a, r);
  }
  function doEl(el) {
    if (el.closest(NO)) return;
    for (const a of ATTRS) if (el.hasAttribute(a)) doAttr(el, a);
    const w = document.createTreeWalker(el, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
      acceptNode: x => (x.nodeType === 1 && x.matches(NO) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    let x;
    while ((x = w.nextNode())) {
      if (x.nodeType === 3) doText(x);
      else for (const a of ATTRS) if (x.hasAttribute(a)) doAttr(x, a);
    }
  }
  new MutationObserver(ms => {
    for (const m of ms) {
      if (m.type === "childList") m.addedNodes.forEach(n => (n.nodeType === 3 ? doText(n) : n.nodeType === 1 && doEl(n)));
      else if (m.type === "characterData") doText(m.target);
      else if (m.type === "attributes" && m.target.nodeType === 1 && !m.target.closest(NO)) doAttr(m.target, m.attributeName);
    }
  }).observe(document.documentElement, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });

  // Browser-Dialoge und Zahlen/Datum (Quelltext formatiert mit "de-DE")
  for (const f of ["alert", "confirm", "prompt"]) {
    const o = window[f];
    window[f] = (m, ...r) => o.call(window, m == null ? m : window.tr(m), ...r);
  }
  const loc = l => (l === "de-DE" || l === "de" ? "en-US" : l);
  for (const [P, fs] of [[Number.prototype, ["toLocaleString"]], [Date.prototype, ["toLocaleString", "toLocaleDateString", "toLocaleTimeString"]]]) {
    for (const f of fs) { const o = P[f]; P[f] = function (l, opt) { return o.call(this, loc(l), opt); }; }
  }
})();
