// FotoArchiv – Seite „Sicherung“: S3-Ziele, Sicherung im Hintergrund, Wiederherstellen,
// Alben als Webseite auf S3 und Ordner für die Amazon-Photos-App (Server: backup.py, webshare.py)

const bkNum = n => (n || 0).toLocaleString("de-DE");
const bkGB = b => (b || 0) >= 1e12 ? (b / 1e12).toLocaleString("de-DE", { maximumFractionDigits: 2 }) + " TB" : fmtSize(b || 0);
let bkTimer = null;

routes.sicherung = async function () {
  clearMain();
  clearTimeout(bkTimer);
  const page = document.createElement("div");
  page.className = "page bk";
  main.appendChild(page);
  const [cfg, st, shares] = await Promise.all([api("/api/backup"), api("/api/backup/status"), api("/api/web")]);
  const b = cfg.backup;
  const has = cfg.targets.length > 0;
  state.hasS3 = has;
  const tsel = (id, cur) => `<select id="${id}">${cfg.targets.map(t => `<option value="${t.id}"${t.id === cur ? " selected" : ""}>${esc(t.name)}</option>`).join("")}</select>`;
  page.innerHTML = `<h1>Sicherung</h1>
    <div class="card"><h2>☁ Sicherung auf S3</h2>
      ${has ? `<p class="muted">Alle Fotos, Videos und Begleitdateien mit gleicher Ordnerstruktur – ohne FotoArchiv lesbar – plus Katalog
        (Alben, Personen, Bearbeitungen). Nur Neues und Geändertes wird hochgeladen; große Dateien in Teilen. Einmal starten, läuft im Hintergrund
        weiter (auch nach einem Neustart). Gelöschtes bleibt in der Sicherung, außer du schaltest es unten um.</p>
      <div class="row">Ziel ${tsel("bk-target", b.target)}
        <label><input type="checkbox" id="bk-videos"${b.videos ? " checked" : ""}> Videos</label>
        <label title="Ausgeblendete Duplikate mitsichern"><input type="checkbox" id="bk-dups"${b.dups ? " checked" : ""}> Duplikate</label>
        <label title="thumbs.db/previews.db – lassen sich auch neu erzeugen"><input type="checkbox" id="bk-previews"${b.previews ? " checked" : ""}> Vorschaubilder</label></div>
      <div class="row">Tempo höchstens <input type="number" id="bk-limit" min="0" step="10" value="${b.limit_mbit || 0}" style="width:80px"> Mbit/s (0 = unbegrenzt)
        · gleichzeitig <select id="bk-workers">${[1, 2, 4, 6, 8].map(n => `<option${+b.workers === n ? " selected" : ""}>${n}</option>`).join("")}</select> Dateien
        · automatisch <select id="bk-auto">${[[0, "nie"], [6, "alle 6 Stunden"], [24, "täglich"], [168, "wöchentlich"]].map(([v, l]) => `<option value="${v}"${+b.auto_hours === v ? " selected" : ""}>${l}</option>`).join("")}</select></div>
      <div class="row"><label title="Erst beim endgültigen Löschen (Papierkorb leeren) – was nur im Papierkorb liegt, bleibt gesichert">
        <input type="checkbox" id="bk-mirror"${b.mirror_deletes ? " checked" : ""}> Endgültig Gelöschtes auch in der Sicherung löschen</label>
        <span class="muted">${b.mirror_deletes ? "" : "aus: Gelöschtes bleibt in der Sicherung (Schutz vor Versehen)"}</span></div>
      <div id="bk-status"></div><div id="bk-orphans"></div>` : `<p>Noch kein Ziel eingerichtet – unten „+ Ziel hinzufügen“ (Amazon S3 oder eigener S3-Speicher wie StorageGRID, MinIO …).</p>`}
    </div>
    <div class="card"><h2>Ziele</h2>
      <div id="bk-targets">${cfg.targets.map(t => `<div class="bk-t"><b>${esc(t.name)}</b>
        <span class="muted">${esc(t.endpoint || "Amazon S3 " + (t.region || ""))} · Bucket ${esc(t.bucket)}${t.prefix ? " · Ordner " + esc(t.prefix) : ""}${t.storage_class ? " · " + esc(t.storage_class) : ""}</span>
        <span style="flex:1"></span><button data-edit="${t.id}">Bearbeiten</button><button data-del="${t.id}" class="danger">Entfernen</button></div>`).join("") || '<div class="muted">keine</div>'}</div>
      <div class="row" style="margin-top:10px"><button id="bk-add">+ Ziel hinzufügen</button></div>
    </div>
    ${has ? `<div class="card"><h2>⤓ Wiederherstellen</h2>
      <p class="muted">Ordner aus der Sicherung zurückholen – an den Originalplatz (nur was fehlt, nichts wird überschrieben) oder in einen anderen Ordner.
        Abgebrochene Downloads laufen an derselben Stelle weiter.</p>
      <div class="row">Quelle (Sicherung) ${tsel("rs-target", b.target)}</div><div id="rs-browser" class="bk-browser"></div>
      <div class="row" style="margin-top:10px"><button id="rs-cat">Katalog aus der Sicherung …</button>
        <span class="muted">z. B. nach Plattentausch: Alben, Personen und Bearbeitungen zurückholen</span></div></div>
    <div class="card"><h2>🌐 Alben als Webseite</h2>
      <p class="muted">Im Album oben „🌐 Webseite“: legt eine Seite mit allen Fotos (ohne private) im Bucket an. Der Link ist 7 Tage gültig
        und lässt sich erneuern – oder dauerhaft, wenn der Bucket öffentliches Lesen für <code>FotoArchiv-Web/</code> erlaubt.
        Wer den Link öffnet, muss den S3-Speicher erreichen können (bei einem Speicher nur im Firmen-/Heimnetz also nur dort).</p>
      <div id="web-list">${shares.length ? shares.map(s => `<div class="bk-t"><b>${esc(s.title)}</b>
        <span class="muted">${bkNum(s.count)} Dateien · ${s.mode === "public" ? "dauerhaft" : s.expires ? "Link bis " + esc(fmtDate(s.expires.replace("T", " "))) : ""}</span>
        <span style="flex:1"></span>${s.url ? `<button data-copy="${esc(s.url)}">Link kopieren</button><a href="${esc(s.url)}" target="_blank"><button>Öffnen</button></a>` : ""}
        ${s.mode === "link" ? `<button data-renew="${s.id}">Erneuern</button>` : ""}<button data-wdel="${s.id}" class="danger">Entfernen</button></div>`).join("")
        : '<div class="muted">Noch keine.</div>'}</div></div>` : ""}
    <div class="card"><h2>Amazon Photos</h2>
      <p class="muted">Amazon bietet keine offene Schnittstelle mehr – hochladen kann nur die <b>Amazon-Photos-App</b> (Windows/Mac). Prime: Fotos in voller
        Auflösung unbegrenzt, Videos nur 5 GB. Verwalten (Alben usw.) geht nur in Amazon Photos selbst.</p>
      <p><b>Ganze Bibliothek:</b> in der App unter „Sicherung“ diese Ordner hinzufügen und „nur Fotos“ wählen:</p>
      <div id="amz-folders" class="muted">lädt …</div>
      <p><b>Nur Auswahl:</b> FotoArchiv kopiert ein Album oder die Favoriten (Bearbeitungen angewendet) in einen eigenen Ordner, den die App sichert –
        bei jedem Lauf nur Neues. Im Album oben „Amazon“; hier für die Favoriten:</p>
      <div class="row"><button id="amz-fav">★ Favoriten in den Amazon-Ordner</button><span class="muted" id="amz-dest"></span></div></div>`;

  // Sicherung: Einstellungen speichern bei jeder Änderung
  const saveSettings = () => api("/api/backup/settings", {
    target: $("#bk-target").value, videos: $("#bk-videos").checked, dups: $("#bk-dups").checked, previews: $("#bk-previews").checked,
    limit_mbit: +$("#bk-limit").value || 0, workers: +$("#bk-workers").value, auto_hours: +$("#bk-auto").value,
    mirror_deletes: $("#bk-mirror").checked,
  }).then(() => toast("Gespeichert"));
  ["#bk-target", "#bk-videos", "#bk-dups", "#bk-previews", "#bk-limit", "#bk-workers", "#bk-auto", "#bk-mirror"].forEach(s => { const e = $(s, page); if (e) e.onchange = saveSettings; });
  if (has) {
    renderBkStatus(st);
    api("/api/backup/orphans?target=" + encodeURIComponent(b.target)).then(o => {
      const box = $("#bk-orphans");
      if (!box || !o.count) return;
      box.innerHTML = `<div class="docscan" style="margin-top:10px"><b>${bkNum(o.count)} Dateien (${bkGB(o.size)}) liegen nur noch in der Sicherung</b>
        – in FotoArchiv gelöscht oder auf der Platte nicht mehr vorhanden.
        <details><summary class="muted">Beispiele</summary><div class="muted bk-cur">${o.sample.map(esc).join("<br>")}</div></details>
        <div class="row" style="margin-top:6px"><button id="bk-prune" class="danger">In der Sicherung löschen …</button>
        <span class="muted">sonst bleiben sie dort und lassen sich unter „Wiederherstellen“ zurückholen</span></div></div>`;
      $("#bk-prune").onclick = async () => {
        if (!await confirmBox(`${bkNum(o.count)} Dateien (${bkGB(o.size)}) endgültig aus der S3-Sicherung löschen? Das lässt sich nicht rückgängig machen.`)) return;
        const r = await api("/api/backup/orphans", { target: b.target }).catch(e => ({ error: e.message }));
        if (r.error || !r.ok) return toast(r.error || "Es läuft schon etwas – bitte warten", 8000);
        box.innerHTML = "";
        pollBk();
      };
    }).catch(() => {});
  }

  $("#bk-add", page).onclick = () => targetDialog();
  $$("[data-edit]", page).forEach(x => x.onclick = () => targetDialog(cfg.targets.find(t => t.id === x.dataset.edit)));
  $$("[data-del]", page).forEach(x => x.onclick = async () => {
    if (!await confirmBox("Ziel entfernen? Die Dateien im Bucket bleiben unangetastet, FotoArchiv vergisst nur die Zugangsdaten.")) return;
    await api("/api/backup/target/delete", { id: x.dataset.del });
    route();
  });
  if (has) {
    const rsT = $("#rs-target", page);
    const browse = path => restoreBrowse(rsT.value, path);
    rsT.onchange = () => browse("");
    browse("");
    $("#rs-cat", page).onclick = () => catalogDialog(rsT.value);
    $$("[data-copy]", page).forEach(x => x.onclick = async () => { try { await navigator.clipboard.writeText(x.dataset.copy); toast("Link kopiert"); } catch { prompt("Link:", x.dataset.copy); } });
    $$("[data-renew]", page).forEach(x => x.onclick = async () => {
      try { await api(`/api/web/${x.dataset.renew}/renew`, {}); toast("Link erneuert – wieder 7 Tage gültig"); route(); } catch (e) { toast(e.message, 8000); }
    });
    $$("[data-wdel]", page).forEach(x => x.onclick = async () => {
      if (!await confirmBox("Webseite löschen? Der Link funktioniert danach nicht mehr. Die Fotos in FotoArchiv bleiben.")) return;
      const r = await api(`/api/web/${x.dataset.wdel}/delete`, {});
      toast(`Entfernt (${bkNum(r.count)} Dateien im Bucket gelöscht)`);
      route();
    });
  }
  api("/api/amazon").then(a => {
    const box = $("#amz-folders", page);
    if (!box) return;
    box.classList.remove("muted");
    box.innerHTML = `<table class="bk-table"><tr><th>Ordner</th><th>Fotos</th><th>Videos</th><th></th></tr>${a.folders.map(f => `<tr>
      <td title="${esc(f.path)}">${esc(f.folder)}</td><td>${bkNum(f.photos)} <small class="muted">${bkGB(f.photo_bytes)}</small></td>
      <td>${bkNum(f.videos)} <small class="muted">${bkGB(f.video_bytes)}</small></td><td><button data-cp="${esc(f.path)}" title="Pfad kopieren">Pfad</button></td></tr>`).join("")}</table>`;
    $$("[data-cp]", box).forEach(x => x.onclick = async () => { try { await navigator.clipboard.writeText(x.dataset.cp); toast("Pfad kopiert: " + x.dataset.cp); } catch { prompt("Pfad:", x.dataset.cp); } });
    $("#amz-dest", page).textContent = "Ordner: " + a.dest;
  }).catch(() => {});
  $("#amz-fav", page).onclick = async () => {
    const r = await api("/api/amazon/sync", { fav: true }).catch(e => ({ error: e.message }));
    if (r.error) return toast(r.error, 8000);
    pollShare(true);
  };
};

function renderBkStatus(st) {
  const box = $("#bk-status");
  if (!box) return;
  const pct = st.bytes_total ? Math.min(100, 100 * st.bytes_done / st.bytes_total) : 0;
  const what = { backup: "Sicherung", restore: "Wiederherstellen", catalog: "Katalog laden", prune: "Aufräumen" }[st.kind] || "Sicherung";
  if (st.running) {
    box.innerHTML = `<div class="docscan"><b>${what} läuft – ${esc(st.phase)}</b>
      ${st.files_total ? `<div class="progress"><i style="width:${pct.toFixed(1)}%"></i></div>
      ${bkNum(st.files_done)} von ${bkNum(st.files_total)} Dateien · ${bkGB(st.bytes_done)} von ${bkGB(st.bytes_total)}
      ${st.rate ? ` · ${(st.rate * 8 / 1e6).toLocaleString("de-DE", { maximumFractionDigits: 0 })} Mbit/s` : ""}${st.eta ? ` · noch ca. ${fmtEta(st.eta)}` : ""}
      ${st.files_skip ? `<br><span class="muted">${bkNum(st.files_skip)} Dateien waren schon gesichert</span>` : ""}` : ""}
      ${st.current.length ? `<div class="muted bk-cur">${st.current.map(esc).join("<br>")}</div>` : ""}
      ${st.error_count ? `<div class="bk-err">${bkNum(st.error_count)} Fehler, z. B. ${esc(st.errors[0])}</div>` : ""}
      <div class="row" style="margin-top:8px"><button id="bk-stop">Anhalten</button><span class="muted">Du kannst weiterarbeiten; nach einem Neustart geht es weiter.</span></div></div>`;
    $("#bk-stop").onclick = async () => { await api("/api/backup/stop", {}); toast("Wird angehalten …"); };
  } else {
    box.innerHTML = `<div class="row" style="margin-top:6px"><button class="primary" id="bk-start">☁ Sicherung starten</button>
      <span class="muted">${st.saved_files ? `${bkNum(st.saved_files)} Dateien (${bkGB(st.saved_bytes)}) gesichert` : "noch nichts gesichert"}${st.last_backup ? ` · zuletzt komplett: ${esc(fmtDate(st.last_backup.replace("T", " ")))}` : ""}</span></div>
      ${st.message || st.last_message ? `<div class="muted" style="margin-top:6px">${esc(st.message || st.last_message)}</div>` : ""}
      ${st.error_count ? `<details class="bk-err"><summary>${bkNum(st.error_count)} Fehler</summary>${st.errors.map(esc).join("<br>")}</details>` : ""}`;
    $("#bk-start").onclick = async () => {
      const r = await api("/api/backup/start", {}).catch(e => ({ error: e.message }));
      if (r.error) return toast(r.error, 9000);
      pollBk();
    };
  }
  if (st.running) bkTimer = setTimeout(pollBk, 2000);
}
async function pollBk() {
  clearTimeout(bkTimer);
  if (!location.hash.startsWith("#/sicherung")) return;
  const st = await api("/api/backup/status").catch(() => null);
  if (st) renderBkStatus(st);
  if (st && !st.running && st.kind === "restore") route();
}

function targetDialog(t) {
  t = t || { addressing: "auto", verify: true };
  const v = k => esc(t[k] == null ? "" : t[k]);
  const verifyVal = t.verify === "chain" ? "chain" : t.verify === false ? "0" : "1";
  const p = modal(`<div class="bk-form"><b>${t.id ? "Ziel bearbeiten" : "Neues Sicherungsziel"}</b>
    <p class="muted" style="margin:0 0 4px">Dieselben Angaben wie im S3 Browser: Server-Adresse, Bucket und die beiden Schlüssel.</p>
    <label>Server (Endpunkt) <input id="tf-endpoint" value="${v("endpoint")}" placeholder="z. B. https://s3.firma.de – leer = Amazon S3"></label>
    <label>Bucket <input id="tf-bucket" value="${v("bucket")}"></label>
    <label>Zugangsschlüssel <input id="tf-access" value="${v("access_key")}" autocomplete="off" placeholder="Access Key"></label>
    <label>Geheimer Schlüssel <input id="tf-secret" type="password" autocomplete="off" data-lpignore="true" data-1p-ignore spellcheck="false" placeholder="${t.secret_set ? "gespeichert – leer lassen zum Behalten" : "Secret Key"}"></label>
    <label>Ordner im Bucket <input id="tf-prefix" value="${t.id ? v("prefix") : "FotoArchiv"}" placeholder="leer = oberste Ebene"></label>
    <details class="bk-adv"${t.id && (t.region || t.sig === "v2" || t.addressing !== "auto" || t.verify !== true || t.ca_file || t.storage_class || t.sse) ? " open" : ""}><summary>Erweitert</summary>
    <label>Name <input id="tf-name" value="${v("name")}" placeholder="frei wählbar, sonst der Bucket"></label>
    <label>Region <input id="tf-region" value="${v("region")}" placeholder="nur Amazon nötig, z. B. eu-central-1"></label>
    <label>Signatur <select id="tf-sig"><option value="v4"${t.sig !== "v2" ? " selected" : ""}>V4 (Standard)</option>
      <option value="v2"${t.sig === "v2" ? " selected" : ""}>V2 (ältere Zugänge – wie „Signature V2“ im S3 Browser)</option></select></label>
    <label>Adressierung <select id="tf-addr">${[["auto", "automatisch"], ["path", "Pfad (host/bucket) – StorageGRID, MinIO"], ["virtual", "virtuell (bucket.host)"]].map(([k, l]) => `<option value="${k}"${t.addressing === k ? " selected" : ""}>${l}</option>`).join("")}</select></label>
    <label>Zertifikat <select id="tf-verify"><option value="1"${verifyVal === "1" ? " selected" : ""}>prüfen</option>
      <option value="chain"${verifyVal === "chain" ? " selected" : ""}>prüfen, aber anderen Namen erlauben</option>
      <option value="0"${verifyVal === "0" ? " selected" : ""}>nicht prüfen (selbst signiert)</option></select></label>
    <label>Eigene Zertifizierungsstelle <input id="tf-ca" value="${v("ca_file")}" placeholder="optional: Pfad zur .pem/.crt-Datei"></label>
    <label>Speicherklasse <select id="tf-class">${[["", "Standard des Speichers"], ["STANDARD", "STANDARD"], ["STANDARD_IA", "STANDARD_IA (selten genutzt)"], ["GLACIER_IR", "GLACIER_IR (Archiv, sofort lesbar)"], ["DEEP_ARCHIVE", "DEEP_ARCHIVE (am günstigsten, Abruf bis 12 h)"]].map(([k, l]) => `<option value="${k}"${(t.storage_class || "") === k ? " selected" : ""}>${l}</option>`).join("")}</select></label>
    <label class="chk"><input type="checkbox" id="tf-sse"${t.sse ? " checked" : ""}> Verschlüsselung im Speicher (SSE)</label></details>
    <div class="muted" id="tf-msg"></div><div id="tf-fix" class="row"></div>
    <div class="row"><button id="tf-test">Verbindung testen</button><span style="flex:1"></span><button class="primary" id="tf-save">Speichern</button><button id="tf-no">Abbrechen</button></div></div>`);
  const data = () => ({
    id: t.id, name: $("#tf-name", p).value.trim(), endpoint: $("#tf-endpoint", p).value.trim(), region: $("#tf-region", p).value.trim(),
    bucket: $("#tf-bucket", p).value.trim(), prefix: $("#tf-prefix", p).value.trim(), access_key: $("#tf-access", p).value.trim(),
    secret_key: $("#tf-secret", p).value, addressing: $("#tf-addr", p).value,
    verify: { "1": true, "0": false, chain: "chain" }[$("#tf-verify", p).value],
    ca_file: $("#tf-ca", p).value.trim(), storage_class: $("#tf-class", p).value, sse: $("#tf-sse", p).checked,
    sig: $("#tf-sig", p).value,
  });
  const msg = (text, ok) => { const m = $("#tf-msg", p); m.textContent = text; m.style.color = ok ? "#7c7" : "#e77"; };
  $$("input", p).forEach(i => i.addEventListener("keydown", e => e.stopPropagation()));
  const test = async () => {
    msg("Teste …", true);
    $("#tf-fix", p).innerHTML = "";
    const r = await api("/api/backup/test", data()).catch(e => ({ error: e.message }));
    if (r.ok) {
      if (r.region) $("#tf-region", p).value = r.region;  // passende Region gefunden und eingetragen
      if (r.sig) $("#tf-sig", p).value = r.sig;  // Zugang braucht Signatur V2
      return msg("✓ Verbindung klappt – " + r.url + (r.region ? ` (Region ${r.region})` : "") + (r.sig ? " (Signatur V2)" : ""), true), true;
    }
    msg(r.error || "Fehler");
    if (r.cert_mismatch) {  // Zertifikat lautet auf anderen Namen: passende Adresse oder "Namen nicht prüfen" anbieten
      const fix = $("#tf-fix", p);
      fix.innerHTML = (r.suggest ? `<button class="primary" id="tf-sug">Adresse ${esc(r.suggest)} verwenden</button>` : "") +
        `<button id="tf-chain">Trotzdem verbinden (Zertifikat prüfen, Namen nicht – wie S3 Browser)</button>`;
      const sug = $("#tf-sug", p);
      if (sug) sug.onclick = () => { $("#tf-endpoint", p).value = r.suggest; test(); };
      $("#tf-chain", p).onclick = () => { $("#tf-verify", p).value = "chain"; test(); };
    }
    return false;
  };
  $("#tf-test", p).onclick = test;
  $("#tf-save", p).onclick = async () => {
    if (!t.id && !(await test())) return;  // neues Ziel nur speichern, wenn die Verbindung klappt
    const r = await api("/api/backup/target", data()).catch(e => ({ error: e.message }));
    if (r.error) return msg(r.error);
    closePopover();
    toast("Ziel gespeichert");
    route();
  };
  $("#tf-no", p).onclick = closePopover;
}

async function restoreBrowse(tid, path) {
  const box = $("#rs-browser");
  if (!box) return;
  box.innerHTML = '<div class="muted">lädt …</div>';
  const d = await api(`/api/backup/browse?target=${encodeURIComponent(tid)}&path=${encodeURIComponent(path)}`).catch(e => ({ error: e.message }));
  if (d.error) { box.innerHTML = `<div class="bk-err">${esc(d.error)}</div>`; return; }
  const parts = path ? path.split("/") : [];
  box.innerHTML = `<div class="crumbs"><a data-p="">Sicherung (oberste Ebene)</a>${parts.map((x, i) => ` › <a data-p="${esc(parts.slice(0, i + 1).join("/"))}">${esc(x)}</a>`).join("")}</div>
    <div class="bk-list">${d.folders.map(f => `<div data-p="${esc((path ? path + "/" : "") + f)}">📁 ${esc(f)}</div>`).join("") || ""}
      ${d.file_count ? `<div class="muted">${bkNum(d.file_count)} ${d.file_count === 1 ? "Datei" : "Dateien"} direkt in diesem Ordner</div>` : ""}
      ${!d.folders.length && !d.file_count ? '<div class="muted">leer – noch nichts gesichert?</div>' : ""}</div>
    ${path ? `<div class="row"><button class="primary" id="rs-orig">„${esc(parts[parts.length - 1])}“ an den Originalplatz zurückholen</button>
      <button id="rs-other">In anderen Ordner laden …</button></div>` : '<div class="muted">Ordner anklicken, um ihn zurückzuholen.</div>'}`;
  $$("[data-p]", box).forEach(x => x.onclick = () => restoreBrowse(tid, x.dataset.p));
  const go = async dest => {
    const r = await api("/api/backup/restore", { target: tid, path, dest }).catch(e => ({ error: e.message }));
    if (r.error || !r.ok) return toast(r.error || "Es läuft schon eine Sicherung oder Wiederherstellung", 8000);
    toast("Wiederherstellen läuft – Fortschritt oben");
    scrollTo(0, 0);
    pollBk();
  };
  const o = $("#rs-orig", box), oth = $("#rs-other", box);
  if (o) o.onclick = async () => { if (await confirmBox(`Alle in „${path}“ fehlenden Dateien aus der Sicherung zurückholen? Vorhandene Dateien bleiben unverändert.`)) go(""); };
  if (oth) oth.onclick = async () => { const dir = await browseFs(""); if (dir) go(dir); };
}

async function catalogDialog(tid) {
  const list = await api("/api/backup/catalogs?target=" + encodeURIComponent(tid)).catch(e => ({ error: e.message }));
  if (list.error) return toast(list.error, 8000);
  if (!list.length) return toast("Kein Katalog in der Sicherung – erst eine Sicherung ganz durchlaufen lassen.");
  const p = modal(`<div style="max-width:520px"><b>Katalog aus der Sicherung übernehmen</b>
    <p class="muted">Ersetzt Alben, Personen, Bearbeitungen, Ereignisse usw. durch den Stand der Sicherung. Der bisherige Katalog bleibt als
      <code>data/catalog-vorher-….db</code> erhalten. FotoArchiv startet danach neu.</p>
    ${list.map((c, i) => `<label class="row"><input type="radio" name="cat" value="${esc(c.key)}"${i === 0 ? " checked" : ""}> ${esc(c.name)}
      <small class="muted">${fmtSize(c.size)} · ${esc((c.modified || "").slice(0, 16).replace("T", " "))}</small></label>`).join("")}
    <div class="row"><button class="primary" id="cat-ok">Übernehmen und neu starten</button><button id="cat-no">Abbrechen</button></div></div>`);
  $("#cat-no", p).onclick = closePopover;
  $("#cat-ok", p).onclick = async () => {
    const key = $("input[name=cat]:checked", p).value;
    closePopover();
    const r = await api("/api/backup/catalog", { target: tid, key }).catch(e => ({ error: e.message }));
    if (r.error) return toast(r.error, 8000);
    toast("Katalog wird geladen – danach startet FotoArchiv neu und lädt die Seite selbst.", 60000);
    const wait = async () => {
      try { await api("/api/backup/status"); setTimeout(wait, 2000); } catch { setTimeout(async function back() {
        try { await api("/api/ping"); location.hash = "#/start"; location.reload(); } catch { setTimeout(back, 1500); } }, 3000); }
    };
    setTimeout(wait, 2000);
  };
}

// Album, Ereignis oder Auswahl als Webseite auf S3; Album/Favoriten als Kopie für Amazon Photos
function albumWebDialog(album) { return webDialog({ album: album.id }, album.name); }
async function webDialog(src, title) {
  const cfg = await api("/api/backup");
  if (!cfg.targets.length) { toast("Erst unter „Sicherung“ ein S3-Ziel einrichten."); location.hash = "#/sicherung"; return; }
  const p = modal(`<div style="max-width:520px"><b>🌐 „${esc(title)}“ als Webseite auf S3</b>
    <div class="row">Ziel <select id="wa-t">${cfg.targets.map(t => `<option value="${t.id}"${t.id === cfg.backup.target ? " selected" : ""}>${esc(t.name)}</option>`).join("")}</select></div>
    <label class="row"><input type="radio" name="wm" value="link" checked> Privater Link, 7 Tage gültig (erneuerbar) – keine Freigabe am Bucket nötig</label>
    <label class="row"><input type="radio" name="wm" value="public"> Dauerhafter Link – Bucket muss öffentliches Lesen für FotoArchiv-Web/ erlauben</label>
    <div id="wa-pub" class="muted" hidden></div>
    <label class="row"><input type="checkbox" id="wa-v" checked> Videos mitnehmen (MP4/MOV)</label>
    <p class="muted">Private Fotos kommen nie hinein. Fotos werden auf 2048 px verkleinert, Bearbeitungen angewendet.</p>
    <div class="row"><button class="primary" id="wa-ok">Webseite anlegen</button><button id="wa-no">Abbrechen</button></div></div>`);
  $("#wa-no", p).onclick = closePopover;
  $$("input[name=wm]", p).forEach(r => r.onchange = async () => {
    const box = $("#wa-pub", p);
    box.hidden = r.value !== "public" || !r.checked;
    if (box.hidden) return;
    box.textContent = "prüfe Bucket-Richtlinie …";
    const d = await api("/api/web/public?target=" + $("#wa-t", p).value).catch(e => ({ error: e.message }));
    if (d.error) { box.textContent = d.error; return; }
    if (d.has_ours) { box.textContent = "✓ Öffentliches Lesen für FotoArchiv-Web ist eingerichtet."; return; }
    box.innerHTML = d.existing == null
      ? `Der Bucket hat noch keine Richtlinie. <button id="wa-apply">Lesen für FotoArchiv-Web/ freigeben</button> (nur dieser Ordner)`
      : `Der Bucket hat schon eine Richtlinie – FotoArchiv ändert sie nicht. Bitte im Speicher diesen Eintrag ergänzen:<pre class="bk-pre">${esc(d.policy)}</pre>`;
    const ap = $("#wa-apply", box);
    if (ap) ap.onclick = async () => {
      const r = await api("/api/web/public", { target: $("#wa-t", p).value }).catch(e => ({ error: e.message }));
      box.textContent = r.error ? r.error : "✓ Freigegeben.";
    };
  });
  $("#wa-ok", p).onclick = async () => {
    const mode = $("input[name=wm]:checked", p).value;
    const r = await api("/api/web", Object.assign({ title, target: $("#wa-t", p).value, mode, videos: $("#wa-v", p).checked }, src)).catch(e => ({ error: e.message }));
    closePopover();
    if (r.error) return toast(r.error, 8000);
    if (selGrid) selGrid.clearSel();
    toast("Webseite wird angelegt – Fortschritt unten links; der Link steht danach unter „Sicherung“.", 8000);
    pollShare(true);
  };
}
async function albumAmazon(album) {
  if (!await confirmBox(`„${album.name}“ in den Ordner „Amazon Photos“ kopieren (nur Fotos, nur Neues)? Die Amazon-Photos-App muss diesen Ordner sichern.`)) return;
  const r = await api("/api/amazon/sync", { album: album.id }).catch(e => ({ error: e.message }));
  if (r.error) return toast(r.error, 8000);
  pollShare(true);
}
