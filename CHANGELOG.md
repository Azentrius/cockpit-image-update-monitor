# Changelog

Ab v5 entspricht die Versionsnummer dem `?v=` in `plugin/index.html`.

## v6 – 2026-10-12 · Konfiguriert / Installiert / Neueste Version

- **Neue Spalten.** Die Tabelle zeigt jetzt *Konfiguriert* (Tag aus Compose), *Installiert* (echte Version) und *Neueste Version*. Die Digest-Spalte entfällt; der Digest steht im Tooltip.
- **Echte installierte Version**, in dieser Reihenfolge:
  1. Tag
  2. Versions-Variable der App (`PG_VERSION`, `GLITCHTIP_VERSION` …)
  3. Image-Label, nur wenn es den Tag in der Registry gibt
  4. Digest-Abgleich über die Docker-Hub-API bzw. Registry-Manifeste, mit Cache
- **`16.14 → 16.15`** für schon gezogene, noch nicht ausgerollte Images.
- **Vergleich mit der installierten Version** im passenden Schema: `16-alpine` + 16.14 → `16.15-alpine`; Nextcloud `apache` → `N.N.N-apache`.
- **Chip-Form und Legende:**
  - gefüllt = per Update-Knopf erreichbar
  - umrandet = Tag-Änderung nötig
  - gestrichelt = Datenbank-Major
- **Status:** „unbekannt“ statt „aktuell“, wenn die installierte Version nicht ermittelbar ist.
- **Neu:** `lib/diunlib.py` mit der gemeinsamen Logik für Inventar und Radar.

## v5 – 2026-10-12 · Stufe 1: Update, Rollback, Backups

- **Knöpfe:** „Update“ (Diun meldet neuen Build) und „Ausrollen“ (neueres Image schon lokal).
- **Plan-Dialog:**
  - betroffene Services/Container (alle mit demselben Image im Projekt)
  - Datenbanken mit Größe
  - freier Platz
  - Warnung bei geänderter Compose-Datei
- **Ablauf:**
  1. `pg_dumpall` bzw. `mariadb-dump`
  2. Rollback-Tag
  3. `compose pull`
  4. `up -d --no-deps`
  5. Warten auf healthy
  6. Ergebnis; bei Fehler Rollback-Angebot
- **Tab „Backups & Dumps“:** Verlauf mit Rollback, Dumps und Rollback-Images löschen.
- **Inventar** in Python neu: Compose-Zuordnung, „gezogen, nicht ausgerollt“, deutlich weniger Docker-Aufrufe.
- **E2E-Test** im Wegwerf-Projekt.
- **Behoben:** Hilfscontainer mit Postgres-Image (`sh -c …`) wurden als Datenbank erkannt. Jetzt zählt nur ein Container, der den Server startet.
- **Behoben:** Ein spät eintreffender Plan öffnete einen geschlossenen Dialog wieder.

## v4 – 2026-10-12 · Zweisprachig, Voraussetzungen

- Deutsch/Englisch nach Cockpit-Sprache (Rückfall Englisch), auch der Menüeintrag.
- **Panel „Voraussetzungen“** prüft Docker, Diun, skopeo und den Radar und bietet die Installation an (`diun-deps.sh`, überschreibt nie).

## v3 – 2026-10-12 · Ladeanzeige

- Ladeblock beim ersten Laden, Spinner im Aktualisieren-Knopf, gedimmte Tabelle.
- **Behoben:** Watch-/Read-Schleife, die Hunderte Inventar-Prozesse gestartet hat. Jetzt wird nie parallel geladen, und neu geladen wird nur bei geändertem Datei-Tag.

## v2 – 2026-10-12 · Versions-Radar

- `diun-versions.py` + systemd-Timer (alle 6 h): neuere Versions-Tags (Patch/Minor/Major) per `skopeo list-tags`; Datenbank-Majors als graue Info.
- **Sicherheit:** Alles, was als root läuft, liegt nach `/usr/local/lib/diun-radar` umgezogen, weg aus dem vom Diun-Container beschreibbaren `/var/lib/diun`.

## v1 · Grundversion

- Tabelle der laufenden Container mit Diun-Meldungen (neuer Digest für denselben Tag), „Erledigt“-Markierung.
