# CLAUDE.md – Hinweise für die Arbeit an diesem Repo

## Worum es geht

Cockpit-Addon „Image-Updates monitor“. Es zeigt für jeden laufenden Docker-Container den konfigurierten Tag, die tatsächlich installierte Version und neuere Versionen. Updates mit gleichem Tag spielt es mit DB-Dump und Rollback ein.

- Überblick und Bedienung: `README.md`
- Schnittstellen und Formate: `docs/INTERNALS.md`
- Nächste Schritte: `ROADMAP.md`

## Zusammenarbeit

- Mit dem Nutzer **auf Deutsch** sprechen: erst das große Bild in wenigen Sätzen, dann Details; Empfehlung zuerst, dann Begründung.
- Code, Kommentare und Commit-Nachrichten auf **Englisch** (wie der bestehende Code). UI-Texte immer **DE und EN**.
- Bei Änderungen, die auf dem Server als root laufen, kurz sagen, was der Nutzer verstehen muss, um dafür geradezustehen.

## Aufbau

```
plugin/   Cockpit-Paket, installiert nach /usr/share/cockpit/diun/   (Vanilla-JS, KEIN Build-Schritt)
lib/      root-Helfer, installiert nach /usr/local/lib/diun-radar/
          diunlib.py          gemeinsame Logik (Tag-Schema, Versions-Variablen, reach)
          diun-inventory.py   Inventar (bei jedem Laden des Plugins)
          diun-versions.py    Versions-Radar (systemd-Timer alle 6 h) -> /var/lib/diun-radar/versions.json
          diun-update.py      plan / update / rollback / backups / delete-*
          diun-deps.sh        check / install skopeo|diun|radar / start diun (enthält auch die systemd-Units und den Diun-Stack)
install.sh                    Installer (DESTDIR, --dry-run, sichert die Vorversion)
test/                         Offline-Tests + e2e-server.sh
tools/preview/                Plugin im Browser mit Testdaten, Screenshots
```

## Befehle

```bash
cd test && npm ci && ./run-all.sh              # alle Offline-Tests (Python + jsdom), muss grün sein
python3 tools/preview/build.py                 # Vorschau: tools/preview/out/diun/index.html?lang=de
python3 tools/preview/shoot.py /tmp/shots      # Screenshots (playwright + chromium), prüft JS-Fehler + Handy-Breite
DESTDIR=/tmp/stage ./install.sh                # Installer trocken in ein Staging-Verzeichnis
```

## Regeln (nicht verhandelbar)

1. **Root-Code nur in `lib/`** und installiert nur nach `/usr/local/lib/diun-radar` (root-owned).
   - Nie etwas aus `/var/lib/diun` ausführen oder importieren: Das Verzeichnis ist im Diun-Container beschreibbar.
2. **Feste Verben.** Das Plugin ruft Helfer nur mit festen Befehlen auf. Jedes neue Argument wird im Helfer per Regex geprüft (Container-Namen, Pfade mit `realpath`, Tags). Neue Verben auch in `docs/INTERNALS.md` eintragen.
3. **Vorher zeigen, dann tun.** Verändernde Aktionen: Plan (lesend) → Bestätigung → Live-Ausgabe → `@@RESULT`.
   - Was scheitern kann, bricht **vor** der Änderung ab (Exit 5).
   - Sperren mit `flock` (Exit 75).
4. **Nichts überschreiben**, was der Nutzer angelegt hat (Compose-Dateien, `/opt/diun`, Units). Wenn es doch nötig ist (Stufe 2): byte-genaue Sicherung vorher, Wiederherstellung im Rollback.
5. **Browser ohne Netz.** Keine CDNs, Webfonts oder externen Bilder; Cockpits strikte CSP bleibt. Registry- und GitHub-Zugriffe nur serverseitig (Radar).
6. **Zwei Sprachen.** Jeder neue Text kommt in `I18N.en` **und** `I18N.de` in `plugin/diun.js`, Helfer-Meldungen über `say(de, en)` / `msg de en`. Die Tests prüfen, dass in der englischen Oberfläche nichts Deutsches steht.
7. **Formate sind Verträge.** Wird Inventar, `versions.json`, `plan`, `backups` oder `@@RESULT` geändert, dann zusammen anpassen:
   - Plugin
   - Fixtures in `test/fixtures/`
   - Tests
   - `docs/INTERNALS.md`

   Alte Felder tolerieren, denn Radar-Daten können eine Version älter sein als das Plugin.
8. **Tests nie an Produktivcontainern.** Ende-zu-Ende nur über `test/e2e-server.sh` (Wegwerf-Projekt `imgupd-test`, räumt auf). Neue Abläufe bekommen dort einen Abschnitt.
9. **Kein Build-Schritt, keine Abhängigkeiten zur Laufzeit.** Python nur Standardbibliothek (≥ 3.8), Shell POSIX `sh` bzw. `bash`, JS ohne Framework. `node_modules` nur für Tests.

## Auf einen Server bringen

1. `cd test && ./run-all.sh` grün.
2. Auf dem Server: Repo aktualisieren, `sudo ./install.sh --dry-run`, dann `sudo ./install.sh`. Die Vorversion landet in `/usr/local/lib/diun-radar/backup-<stamp>/`.
3. Hat sich das Radar-Format geändert: `install.sh` startet den Radar selbst. Ergebnis mit `journalctl -u diun-radar.service -n 5` prüfen.
4. Im Browser Cockpit neu laden. Bei Änderungen an `diun.js`/`diun.css` die `?v=`-Nummer in `plugin/index.html` erhöhen (Cache).
5. `CHANGELOG.md` ergänzen.

## Nützliches Wissen

- **Docker mit containerd-Image-Store:** Image-ID = Index-Digest. `docker rmi` auf den **letzten** Tag eines benutzten Images wird verweigert; mit `-f` wird nur der Tag entfernt.
- **Compose-Labels**, die die Helfer nutzen:
  - `com.docker.compose.project`
  - `.service`
  - `.project.working_dir`
  - `.project.config_files`
  - `.project.environment_file`
  - `.config-hash`
- **Docker-Hub-API** (`hub.docker.com/v2/repositories/<ns>/<repo>/tags?ordering=last_updated`) liefert Tags mit Digests und zählt nicht gegen das Pull-Limit. `skopeo list-tags` ist ebenfalls kein Pull, `skopeo inspect` schon.
- **`diun-radar.service`** läuft gehärtet (`ProtectSystem=strict`, `ProtectHome=yes`). Er darf nur nach `/var/lib/diun-radar` schreiben und sieht `/root` nicht (keine Registry-Zugangsdaten).
- **Cockpit:**
  - `superuser: "try"` liefert ohne Admin-Rechte Fehler mit `problem: "access-denied"` bzw. Exit 77 aus unseren Helfern.
  - `cockpit.file().watch()` meldet beim eigenen `read()` erneut; deshalb wird nur bei geändertem Tag neu geladen.
