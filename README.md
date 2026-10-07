<p align="center"><img src="app/static/icon.png" width="128" alt="FotoArchiv"></p>

# FotoArchiv

Fotoverwaltung im Browser – ohne Cloud, ohne Abo, ohne Installation. Läuft unter Windows und macOS
direkt aus einem Ordner (auch von einer externen Festplatte) und lässt die Fotos, wo sie sind.

- Zeitleiste, Kalender, Ordner, Karte, Favoriten, Volltextsuche (Ort, Monat, Kamera, Stichwort …)
- **Gesichtserkennung** auf dem eigenen Computer: Gesichter werden gruppiert, einmal benennen genügt
- Alben, Ereignisse, private (passwortgeschützte) Fotos
- **Duplikate** finden und aufräumen, Papierkorb zum Wiederherstellen
- Import aus iCloud, Google Takeout, Ordnern, ZIP-Dateien
- Diashow (auch als Video), Teilen, aufs Handy per QR-Code, Samsung-Fernseher / The Frame
- Übernahme aus Mylio (optional)

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

Fremde Bestandteile und ihre Lizenzen: [DRITTANBIETER.md](DRITTANBIETER.md).
