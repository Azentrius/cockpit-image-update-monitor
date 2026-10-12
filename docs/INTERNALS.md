# Interna: Schnittstellen, Formate, Abläufe

Referenz für alle, die am Code weiterarbeiten. Die Helfer sind die **einzige** Brücke zwischen Browser und root. Wer ein Format ändert, ändert Helfer, Plugin, Fixtures (`test/fixtures/`) und dieses Dokument zusammen.

## Pfade

| Pfad | Inhalt | Besitzer / Rechte |
|---|---|---|
| `/usr/share/cockpit/diun/` | Plugin | root, 644 |
| `/usr/local/lib/diun-radar/` | Helfer (`*.py`, `*.sh` 755, `diunlib.py` 644), Sicherungen von `install.sh` (`backup-*`, 700) | root |
| `/var/lib/diun-radar/versions.json` | Ergebnis des Versions-Radars | root, 644 (StateDirectory) |
| `/var/lib/diun/events.jsonl` | Diun-Ereignisse (vom Notifier **im Container** geschrieben) | im Diun-Container beschreibbar → nur Daten, nie Code |
| `/var/lib/diun/dismissed.json` | vom Plugin „Erledigt“ markierte Diun-Ereignisse | Daten |
| `/var/backups/image-updates/` | `dumps/<projekt>/*.sql.gz`, `operations.jsonl` | root, 700 / 600 |
| `/opt/diun/` | Diun-Stack (`docker-compose.yml`, `diun.yml`), von `diun-deps.sh install diun` | root |
| `/etc/systemd/system/diun-radar.{service,timer}` | Radar alle 6 h (`*-*-* 00/6:17:00`) | root |
| `/run/lock/diun-{inventory,update,deps}.lock` | `flock`-Sperren | root |

## Aufrufe aus dem Plugin

Alle über `cockpit.spawn(argv, { superuser, err, environ: ["DIUN_LANG=de|en"] })`:
- Lesen (Inventar, Check, Plan, Backups): `superuser: "try"`, `err: "message"`.
- Aktionen: `superuser: "require"`, `err: "out"`, Ausgabe wird live gestreamt.

| Helfer | Aufruf | Art | Ausgabe |
|---|---|---|---|
| `diun-inventory.py` | (keine Argumente) | lesen | JSON-Zeilen, siehe unten |
| `diun-deps.sh` | `check` | lesen | ein JSON-Objekt |
| | `install skopeo` / `install diun` / `install radar` / `start diun` | Aktion | Text, zweisprachig |
| `diun-update.py` | `plan <container>` | lesen | ein JSON-Objekt |
| | `update <container> [--no-dump]` | Aktion | Text + letzte Zeile `@@RESULT {…}` |
| | `rollback <op-id>` | Aktion | Text + `@@RESULT {…}` |
| | `backups` | lesen | ein JSON-Objekt |
| | `delete-dump <projekt>/<datei>.sql.gz` | Aktion | Text |
| | `delete-image <repo>:rollback-JJJJMMTT-HHMMSS` | Aktion | Text (`docker rmi`, ohne `-f`) |
| `diun-versions.py` | `--output FILE` (systemd), `--table`, ohne Argument JSON-Zeilen | Timer / Diagnose | – |

Eingaben werden per Regex geprüft:

| Eingabe | Regex |
|---|---|
| Container-Name | `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$` |
| Vorgangs-ID | `^\d{8}-\d{6}-<name>$` |
| Dump-Pfad | `^<name>/\d{8}-\d{6}_<name>\.sql\.gz$` + `realpath`-Prüfung |
| Rollback-Tag | `^rollback-\d{8}-\d{6}$` |

`DIUN_LANG` wählt nur die Sprache der Textausgabe.

### Exit-Codes (alle Helfer)

| Code | Bedeutung |
|---|---|
| 0 | ok (auch `nochange`) |
| 1 | unerwarteter Fehler eines Unterbefehls (stderr in der Ausgabe) |
| 2 | nicht gefunden / nicht möglich (Container, Vorgang, Rollback-Image weg, nicht aktualisierbar) |
| 3 | abgelehnt (schon zurückgerollt; Datei/Container existiert bei `install`) |
| 4 | nach Update/Rollback laufen die Container nicht stabil |
| 5 | vor jeder Änderung abgebrochen (Dump fehlgeschlagen, zu wenig Platz, DB läuft nicht) |
| 64 | falscher Aufruf |
| 75 | Sperre belegt (läuft schon) |
| 77 | keine root-Rechte |

## Formate

### Inventar – eine Zeile je laufendem Container

```json
{"name": "glitchtip-postgres", "image": "postgres", "tag": "16-alpine",
 "digest": "sha256:16bc…", "imgid": "sha256:16bc…",
 "version": "16.14", "version_src": "env:PG_VERSION",
 "tagid": "sha256:7218…", "pulled": true, "pulled_version": "16.15",
 "project": "glitchtip", "service": "glitchtip-postgres", "managed": true}
```

- `image` ist das Repo ohne Tag, so wie es in der Compose-Datei steht.
- `version_src` ist `tag`, `env:<NAME>` oder `null`. Label- und Digest-Quellen kennt nur der Radar.
- `pulled` bedeutet: Der lokale Tag zeigt auf ein anderes Image als das laufende.
- `managed` bedeutet: Die Compose-Labels für Projekt, Service, Arbeitsverzeichnis und Dateien sind vorhanden.
- Bei Dockers containerd-Image-Store ist `imgid` gleich dem Index-Digest. Das ist normal.

### `versions.json` (Radar)

```json
{"generated": "2026-10-12T02:17:34Z",
 "images": [{
   "image": "postgres", "tag": "16-alpine",
   "containers": ["docuseal-db", "glitchtip-postgres"],
   "db": true,
   "reach": ["patch", "minor"],
   "installed": {
     "sha256:16bc…": {"version": "16.14", "src": "env:PG_VERSION"},
     "sha256:c484…": {"version": "0.30.11", "src": "digest", "tag": "0.30.11", "ts": "…"}
   },
   "versions": {
     "16.14": {"patch": null, "minor": "16.15-alpine", "major": "18.6-alpine",
               "status": "major", "as_tag": "16.14-alpine", "scheme": "N.N-alpine"}
   },
   "by_tag": {"patch": null, "minor": null, "major": "18-alpine", "status": "major"},
   "newest": "18.6",
   "status": "major",
   "checked": "…",
   "stale": true, "error": "…"
 }]}
```

| Feld | Bedeutung |
|---|---|
| `installed` | je **Image-ID**, auch für das neuere, schon gezogene Image. `src` ist `tag`, `env:<NAME>`, `label` oder `digest`. Digest-Treffer sind der Cache für den nächsten Lauf: Treffer dauerhaft, `version: null` wird nach 7 Tagen neu versucht. |
| `versions[<installierte Version>]` | neuere Tags im Schema der installierten Version. `status: "unknown"`, wenn es kein passendes Schema gibt (z. B. pgvector `pg17` mit `PG_VERSION` 17.10). |
| `by_tag` | Vergleich mit dem konfigurierten Tag selbst. Nur wenn der Tag Zahlen enthält; das Plugin nutzt ihn als Rückfall. |
| `reach` | Stufen, die der konfigurierte Tag per Update-Knopf liefert. |
| `newest` | höchster schlichter Versions-Tag. Wird angezeigt, wenn die installierte Version unbekannt ist. |
| `status` | `major`, `minor`, `patch`, `current`, `unknown` oder `error`, zum Sortieren und für die Tabelle. |
| `stale` / `error` | Registry-Fehler in diesem Lauf. Der Rest des Eintrags stammt aus dem letzten guten Lauf. |

So rechnet das Plugin:

```
installiert = inventar.version  ||  versions.installed[imgid].version
vergleich   = versions.versions[installiert]  (falls fehlt oder "unknown": by_tag)
neuere      = Chips patch/minor/major; gefüllt wenn in reach, sonst umrandet; db+major = grau
```

### `plan` (diun-update.py)

```json
{"container": "glitchtip-web", "image": "glitchtip/glitchtip:latest",
 "updatable": true, "reason": null,
 "project": "glitchtip", "workdir": "/opt/glitchtip",
 "services": ["glitchtip-migrate", "glitchtip-web", "glitchtip-worker"],
 "containers": ["glitchtip-migrate", "glitchtip-web", "glitchtip-worker"],
 "dbs": [{"name": "glitchtip-postgres", "kind": "postgres", "running": true, "size": 162529280},
         {"name": "glitchtip-redis", "kind": "unsupported", "running": true, "size": null}],
 "free_bytes": 84654432256, "config_changed": false, "pulled": false, "old_image": "sha256:…"}
```

- `reason`, wenn nicht aktualisierbar: `not-compose`, `compose-file-missing` (+`missing`), `digest-pinned`, `built-locally` oder `compose-config-error` (+`detail`).
- `services`: alle Services des Projekts mit **demselben** Image-Verweis.
- `dbs`: alle Datenbank-Container des Projekts.
  - `kind`: `postgres` oder `mysql`, wenn der Befehl den Server startet; `unsupported` für redis/valkey/mongo/clickhouse … (kein Dump).
  - `size`: nur Postgres, Summe von `pg_database_size`.
- `config_changed`: `docker compose config --hash` weicht vom Label `com.docker.compose.config-hash` ab. Die Änderung wird mit ausgerollt.

### `@@RESULT` (letzte Zeile von `update` / `rollback`)

```
@@RESULT {"op": "20261012-015025-imgupd-test-db", "status": "ok|nochange|failed",
          "rollback": "repo:rollback-20261012-015025", "dumps": ["projekt/…sql.gz"]}
```

Das Plugin blendet diese Zeile im Log aus und wertet sie aus. Bei `failed` (Exit 4) bietet es den Rollback mit `op` an.

### `operations.jsonl`

Eine JSON-Zeile je Vorgang. Spätere Zeilen mit derselben `id` ergänzen frühere; so markiert ein Rollback den Vorgang mit `rolled_back`, `rollback_ts` und `rollback_status`.

```json
{"id": "20261012-015025-imgupd-test-db", "ts": "…", "kind": "update", "container": "imgupd-test-db",
 "project": "imgupd-test", "workdir": "/root/imgupd-test", "files": ["…/docker-compose.yml"], "env_files": [],
 "services": ["app", "db"], "containers": ["imgupd-test-app", "imgupd-test-db"],
 "image": "imgupd-test/pg:16", "old_id": "sha256:…", "new_id": "sha256:…",
 "rollback": "imgupd-test/pg:rollback-20261012-015025", "dumps": ["imgupd-test/…sql.gz"], "status": "failed"}
```

`backups` liefert die letzten 30 Vorgänge mit zusätzlich `can_rollback`, ohne Pfade und Image-IDs. Dazu kommen `dumps[]` (`path`, `project`, `container`, `size`, `mtime`), `images[]` (`ref`, `id`, `size`, `created`, `in_use`), `dump_bytes`, `free_bytes` und `dir`.

### `diun-deps.sh check`

```json
{"root": true,
 "docker": {"state": "ok|no-cli|no-daemon", "version": "27.x", "compose": true},
 "diun":   {"state": "running|stopped|missing", "managed": true, "events": true, "can_install": false, "can_start": false},
 "skopeo": {"state": "ok|missing", "version": "1.x", "can_install": true},
 "radar":  {"state": "active|inactive|missing", "can_install": true}}
```

- `diun.managed`: `/opt/diun/docker-compose.yml` existiert.
- `diun.events`: `events.jsonl` ist nicht leer.
- `install diun` ist nur auf frischen Systemen möglich: Abbruch, wenn `/opt/diun` oder ein Diun-Container existiert.

### Diun-Ereignis (`events.jsonl`)

```json
{"ts": "2026-10-11T10:00:16Z", "status": "update", "hostname": "docker-host", "provider": "docker",
 "image": "docker.io/ollama/ollama:latest", "digest": "sha256:…", "created": "…",
 "platform": "linux/amd64", "hub_link": "https://hub.docker.com/r/ollama/ollama"}
```

- Geschrieben vom Script-Notifier `diun-notify.sh` im Diun-Container: eine Zeile mit einem einzigen `printf`, damit parallele Notifier sich nicht vermischen.
- Diun prüft alle 6 h (`0 */6 * * *`) mit `firstCheckNotif: true`.

## Plugin-Interna

- **Kein Build-Schritt**, kein Framework, nur `cockpit.js` aus `../base1/`.
- **Sprache:** `../base1/po.js` setzt `cockpit.language`. `de*` → Deutsch, sonst Englisch.
  - Texte stehen in `I18N.en` / `I18N.de` in `diun.js`. Statische Texte im HTML tragen `data-i18n`.
  - Der Menüeintrag wird über `po.manifest.de.js` übersetzt.
- **Laden:** `safeLoad()` lädt nie doppelt; eine Anfrage während des Ladens wird einmal nachgeholt.
  - `cockpit.file().watch()` löst nur bei **geändertem** Tag neu aus.
  - Hintergrund: Am 12.10.2026 hat ein Watch-Read-Kreislauf Hunderte Inventar-Prozesse gestartet.
- **Dialog:** ein `<dialog>` für alles (Installieren, Update, Rollback, Löschen). Ablauf: `dlg.open` → `confirmAndRun` → `dlg.run` → `onDone`.
  - Ein Plan, der eintrifft, nachdem der Dialog geschlossen wurde, wird verworfen (`planSeq`).
- **Sortierung:** Update verfügbar → Ausrollen ausstehend → Neue Version → unbekannt → aktuell.

## Tests

| Datei | Prüft |
|---|---|
| `test/radar_test.py` | `diunlib` (Versionsquellen, Erreichbarkeit), Radar (Schema, Vergleich, Hub-/Registry-Abgleich mit Fakes, Cache, Ausfall) |
| `test/inv_test.py` | Inventar mit Fake-Docker |
| `test/loading.js` | Ladeanzeige, Doppelladen, Fehlerfälle |
| `test/i18n-deps.js` | DE/EN, Voraussetzungs-Panel, Installieren-Dialog |
| `test/update-backups.js` | Update-Knöpfe, Plan-Dialog, Dump-Häkchen, Rollback-Angebot, Backups-Tab, Löschen |
| `test/version-cols.js` | Spalten Konfiguriert / Installiert / Neueste, Chips, Legende |
| `test/harness.js` | druckt die ganze Tabelle (Smoke-Test, auch zum Ansehen) |
| `test/e2e-server.sh` | echter Docker-Host, Wegwerf-Projekt `imgupd-test` |

Die Fixtures in `test/fixtures/` stammen von einem echten Host. Hostnamen sind entfernt; `make_v2.py` erzeugt `inventory3.jsonl` und `versions2.json` aus `inventory2.jsonl`.
