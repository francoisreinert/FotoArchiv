<p align="center"><img src="app/static/icon.png" width="128" alt="FotoArchiv"></p>

# FotoArchiv

Fotoverwaltung im Browser – ohne Cloud, ohne Abo, ohne Installation. Läuft unter Windows und macOS
direkt aus einem Ordner (auch von einer externen Festplatte) und lässt die Fotos, wo sie sind.
Oberfläche auf **Deutsch und Englisch**. *[English summary below](#english)*

- Zeitleiste, Kalender, Ordner, Karte, Favoriten, Volltextsuche (Ort, Monat, Kamera, Stichwort …)
- Betrachter mit **Vollbild** und Blättern; **Diashow** in Reihenfolge alt → neu, neu → alt oder zufällig
- Filter nach Fotos/Videos und **Kamera** (z. B. alle GoPro-Aufnahmen), auch innerhalb von Alben, Jahren, Ordnern
- Mehrere Fotoordner: externe Platten, **NAS**, lokale Ordner – gemeinsam durchsuchbar
- **Fotobearbeitung** (zuschneiden, begradigen, Licht, Farbe, Tiefen/Lichter – Original bleibt unverändert)
- **Videoschnitt** mit Zeitleiste, Übergängen, Originalton und Musik; Rendern auf allen Prozessorkernen
- **Gesichtserkennung** auf dem eigenen Computer: Gesichter werden gruppiert, einmal benennen genügt
- Alben, Ereignisse, private (passwortgeschützte) Fotos; Suche und Filter auch nur innerhalb eines Albums/Ereignisses
- **Titelbild selbst wählen** für Alben, Ereignisse, Ordner, Personen und Kalender-Monate
- **Duplikate** finden und aufräumen, Papierkorb zum Wiederherstellen (große Mengen mit Fortschritt im Hintergrund)
- **360°-Fotos** (GoPro Fusion, Theta, Insta360, Photo Sphere) rundum ansehen – direkt im Browser
- **Belichtungsreihen** (Bracketing) erkennen und als Stapel zeigen – nie als Duplikat behandelt
- **Dokumente aussortieren**: Fotos von Briefen, Rechnungen, Bildschirmfotos werden erkannt (auch nach jedem Import)
  und aus der Zeitleiste genommen; behalten, löschen oder zurück – endgültig Gelöschtes holt kein Import wieder
- **Sicherung auf S3** (Amazon S3 oder eigener S3-Speicher wie StorageGRID/MinIO): im Hintergrund, nur Neues,
  große Dateien in Teilen, fortsetzbar, mit Wiederherstellen; Alben/Ereignisse als **Webseite** im Bucket teilen
- **Archiv-Ansicht im Bucket** – die ganze Sammlung ohne FotoArchiv im Browser ansehen, aufgebaut wie FotoArchiv:
  Übersicht mit „An diesem Tag“, Zeitleiste, Kalender, Alben & Ereignisse, Ordner, Personen, Karte, Favoriten,
  Suche und Filter, Videos, 360°; **private Fotos verschlüsselt** (AES-256, gleiches Passwort wie in FotoArchiv);
  Änderungen werden automatisch nachgezogen. Der Bucket bleibt privat (signierte Links), FotoArchiv warnt,
  wenn der Sicherungsordner ohne Schlüssel lesbar ist, und sperrt ihn auf Knopfdruck
- Datum aus AVCHD-Camcordern (MTS) wird aus dem Videostrom gelesen
- Import aus iCloud (auch mehrere Konten und geteilte Bibliothek), Google Takeout, Ordnern, ZIP-Dateien –
  wahlweise kopieren oder verschieben; Fotos und Videos können in getrennte Ordner
- **Eingangsordner**: Dateien einfach hineinkopieren (auch übers Netz) – FotoArchiv übernimmt sie von selbst,
  erkennt Doppelte, macht aus Unterordnern Alben und hält Live Photos zusammen
- **Zurück nach iCloud** (Windows, mit „iCloud für Windows“): einzelne Fotos oder ganze Alben/Ereignisse hochladen,
  auch automatisch für neue Fotos; danach wird am PC der Speicher wieder freigegeben. **Geteilte iCloud-Alben**
  werden als Alben importiert
- Diashow (auch als Video), Teilen, aufs Handy per QR-Code, Samsung-Fernseher / The Frame
- Ordner für die **Amazon-Photos-App** füllen (Album, Favoriten)
- Übernahme aus Mylio (optional)
- **Deutsch oder Englisch** umschaltbar; Einstellungen (Bibliothek, Import, Sicherung) übersichtlich gruppiert,
  jeder Punkt aufklappbar mit kurzer Erklärung

## So sieht es aus

| Übersicht | Karte |
|---|---|
| ![Übersicht mit Statistik und Fotos pro Jahr](screenshots/uebersicht.png) | ![Karte mit Fotos nach Ort](screenshots/karte.png) |
| **Videoschnitt** | **Bibliothek mit weiteren Fotoordnern (NAS)** |
| ![Videoschnitt mit Projekten und Renderliste](screenshots/videoschnitt.png) | ![Bibliothek: Fotoordner, NAS, Katalog](screenshots/bibliothek.png) |

## Herunterladen und starten

1. Unter **[Releases](../../releases)** die Datei `FotoArchiv.zip` herunterladen und entpacken.
   Den Ordner `FotoArchiv` an einen festen Platz legen (z. B. Dokumente oder die Foto-Festplatte).
2. **Windows:** `Verknüpfung anlegen (Windows).cmd` doppelklicken (Desktop + Startmenü) oder direkt
   `FotoArchiv starten (Windows).cmd`.
   **Mac:** `FotoArchiv.app` – beim ersten Mal Rechtsklick › Öffnen.
3. Im Browser den **Fotoordner wählen** und „Bibliothek aktualisieren“.

Alles Weitere steht in [LIESMICH.txt](LIESMICH.txt).

## Aufbau

| Ordner | Inhalt |
|---|---|
| `app/` | Server (Python, nur Standardbibliothek + Bildverarbeitung) und Oberfläche (`static/`, Vanilla JS) |
| `models/` | Gesichtsmodelle (OpenCV YuNet/SFace) und Ortsdaten (GeoNames) |
| `runtime/` | Python + Pakete je Plattform – **nur im Release-ZIP**, neu bauen mit `tools/build_runtime.py` |
| `tools/` | Laufzeit bauen, Icon erzeugen, Paket bauen, Mylio-Export |
| `data/` | entsteht beim ersten Start: Katalog, Vorschaubilder, Einstellungen (gehört nie ins Repository) |

## Mitmachen

FotoArchiv ist ein privates Freizeitprojekt ohne kommerzielle Absichten – Mitarbeit ist willkommen:

- **Fehler oder Idee?** Unter [Issues](../../issues) beschreiben (gern mit Bildschirmfoto, Windows/Mac-Version).
- **Fragen, Erfahrungen, „ich würde gern mitmachen“:** [Discussions](../../discussions).
- **Selbst etwas verbessert?** Gern als Pull Request. Oberfläche und Texte sind deutsch; der Code ist bewusst
  ohne Frameworks gehalten (Python-Standardbibliothek + Bildverarbeitung, Vanilla JS). Die Texte stehen deutsch
  im Code; die englische Oberfläche kommt aus `app/static/i18n_en.js` (Wörterbuch) und `i18n.js`.

## English

**FotoArchiv** is a photo manager that runs in your browser – no cloud, no subscription, no installation.
It runs on Windows and macOS straight from a folder (also from an external drive) and leaves your photos where
they are. The interface is available in **English and German** (Settings › Library › Language, or open
`http://127.0.0.1:8765/?lang=en`).

- Timeline, calendar, folders, map, favorites, full-text search (place, month, camera, keyword …),
  filter by photo/video and **camera**; viewer with full screen; slideshows (old → new, new → old, shuffle)
- Several photo folders: external drives, **NAS**, local folders – searchable together
- **Photo editing** (crop, straighten, light, color – the original stays untouched) and a **video editor**
  with timeline, transitions, original audio and music, rendering on all CPU cores
- **Face recognition** on your own computer – faces are grouped, naming once is enough
- Albums, events, **private** (password-protected) photos, custom **cover images**
- Find and clean up **duplicates**, trash with restore, **exposure brackets** as stacks, **360° photos**,
  **document photos** (letters, receipts, screenshots) sorted out of the timeline
- **Import** from iCloud (several accounts, shared library), Google Takeout, folders, ZIP files – copy or move;
  an **inbox folder** imports everything you drop into it automatically
- **Back to iCloud** (Windows, via “iCloud for Windows”): upload photos, albums or events, local space is freed
  afterwards; **shared iCloud albums** are imported as albums
- **Backup to S3** (Amazon S3 or S3-compatible storage such as MinIO): in the background, resumable, with
  restore; albums as **web pages**; an **archive view** in the bucket to browse the whole collection without
  FotoArchiv, private photos encrypted (AES-256)
- Samsung TV / The Frame, phone via QR code, Amazon Photos folder, optional import from Mylio

**Download:** get `FotoArchiv.zip` from [Releases](../../releases), unzip, start
`FotoArchiv starten (Windows).cmd` (Windows) or `FotoArchiv.app` (Mac), choose your photo folder and click
“Update library”. The detailed manual (LIESMICH.txt) is in German. Licensed under GPL-3.0.

## Lizenz und Haftung

FotoArchiv steht unter der **GNU General Public License v3.0** ([LICENSE](LICENSE)): Jeder darf es nutzen,
verändern und weitergeben; wer eine veränderte Fassung weitergibt, muss deren Quelltext ebenfalls unter der
GPL-3.0 offenlegen.

Die Software wird unentgeltlich und **ohne jede Gewährleistung** bereitgestellt (siehe Abschnitte 15 und 16
der Lizenz). Sie verändert Fotos nicht, kann sie aber auf Wunsch verschieben oder löschen (Papierkorb) –
bitte wie bei jeder Software regelmäßig Sicherungen anlegen.

Mitgelieferte fremde Bestandteile stehen unter ihren eigenen Lizenzen: Übersicht in
[DRITTANBIETER.md](DRITTANBIETER.md), vollständige Texte im Ordner [Lizenzen](Lizenzen).
Ortsdaten © [GeoNames](https://www.geonames.org) (CC BY 4.0), Kartenbilder © OpenStreetMap-Mitwirkende.
Apple, iCloud, Google, Samsung, The Frame, Amazon, Amazon S3, NetApp StorageGRID und Mylio sind Marken ihrer Inhaber; FotoArchiv ist mit keinem
dieser Unternehmen verbunden.
