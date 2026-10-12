# Ausbaustufen

Stand: 12.10.2026. Jede Stufe ist so geschnitten, dass sie für sich nutzbar ist und eigene Tests mitbringt. Reihenfolge = Empfehlung.

**Leitplanken für jede Stufe** (gelten unverändert weiter):

- **Ort:** Root-Code nur in `/usr/local/lib/diun-radar`, nur feste Verben, Argumente per Regex prüfen.
- **Vorher zeigen:** Jede verändernde Aktion zeigt erst einen Plan, dann die Live-Ausgabe, dann das Ergebnis.
- **Abbrechen vor der Änderung:** Was schiefgehen kann, bricht ab, bevor sich etwas ändert (Dump, Platz, Validierung).
- **Sprache:** DE und EN für jeden Text, Mobilansicht, keine externen Ressourcen im Browser.
- **Tests:** offline in `test/`, End-to-End nur in Wegwerf-Projekten, nie an Produktivcontainern.

---

## Stufe 1 – Update mit gleichem Tag ✅

Erledigt (siehe [CHANGELOG](CHANGELOG.md)):
- Update/Ausrollen mit Plan, DB-Dump (Postgres, MariaDB/MySQL), Rollback-Tag, Health-Wartezeit, Rollback
- Backups-&-Dumps-Tab mit Löschen
- Spalten Konfiguriert / Installiert / Neueste Version

---

## Stufe 1.1 – Feinschliff (klein, als Nächstes)

| # | Thema | Umfang | Fertig, wenn … |
|---|---|---|---|
| 1 | **Radar nach Update anstoßen** | `diun-update.py` startet nach erfolgreichem Update/Rollback `systemctl start --no-block diun-radar.service` (nur wenn die Unit existiert). Das Plugin lädt neu, sobald sich `versions.json` ändert; der Watch ist schon da. | nach dem Update von z. B. ollama (`latest`) die Spalte *Installiert* innerhalb ~1 min die neue Version zeigt statt `?` |
| 2 | **„Radar jetzt prüfen“** | Knopf neben *Aktualisieren*; neues festes Verb (z. B. `diun-update.py radar`), das den Service startet. Anzeige „läuft …“ bis `versions.json` neu ist. | Knopf startet genau einen Lauf; Doppelklick startet keinen zweiten (systemd-Oneshot) |
| 3 | **Aufräumregeln** | Standard: je Container die letzten **3** Dumps behalten, ungenutzte Rollback-Images nach **14 Tagen** löschen. Konfigurierbar in `/etc/image-updates.conf`, root-owned, einfache `KEY=VALUE`-Datei. Läuft nach jedem erfolgreichen Update und täglich per Timer. Im Backups-Tab: „würde gelöscht“-Vorschau und Knopf „Jetzt aufräumen“. | Tests mit Fake-Dateisystem; nie wird ein Dump des **letzten** Updates oder ein Image in Benutzung gelöscht |
| 4 | **Versionen im Verlauf** | `operations.jsonl` bekommt `from_version` / `to_version` (über `diunlib.local_version` bzw. den Radar-Cache). Der Verlauf zeigt „16.14 → 16.15“. | Alte Einträge ohne Felder werden weiter angezeigt |
| 5 | **MariaDB/MySQL Ende-zu-Ende** | `test/e2e-server.sh` um ein Wegwerf-Projekt mit `mariadb:11` (und `mysql:8`) erweitern: Dump enthält Testdaten, Abbruch bei falschem Passwort. | E2E grün für Postgres, MariaDB, MySQL |
| 6 | **Offline-Tests für `diun-update.py`** | Fake-`docker`/`compose` analog `test/inv_test.py`: Plan-Gründe, DB-Erkennung (`kind_of`), Exit-Codes, Pfadprüfung. | `run-all.sh` deckt `diun-update.py` ohne Docker ab |
| 7 | **CI** | GitHub Actions: `test/run-all.sh`, `shellcheck lib/*.sh install.sh test/*.sh`, `python -m pyflakes lib`. | grüner Haken auf jedem Push/PR |
| 8 | **systemd-Units als Dateien** | `diun-radar.service/.timer` liegen heute als Heredoc in `diun-deps.sh`. Nach `systemd/` verschieben; `install.sh` und `diun-deps.sh` nutzen dieselbe Quelle. | eine Quelle der Wahrheit; `diun-deps.sh install radar` funktioniert unverändert |

---

## Stufe 2 – Versionssprünge (Tag in der Compose-Datei ändern)

**Ziel:** Ein Klick auf einen *umrandeten* Chip (z. B. „Patch v2.1.5“ bei konfiguriertem `v2.1.4`) wechselt sicher auf diesen Tag.

**Ablauf**

1. Dialog „Auf `v2.1.5` wechseln?“ mit **Diff-Vorschau** der Compose-Änderung, Plan wie bei Stufe 1 und Hinweis auf Release Notes (ab Stufe 3 mit Inhalt).
2. Neues Verb `diun-update.py retag <container> <ziel-tag>`. Das Ziel wird gegen die Tag-Liste der Registry und das Schema der installierten Version geprüft.
3. **Wo der Tag steht** (Compose-Label `config_files`, ggf. mehrere Dateien/Overrides):
   - `image: repo:v2.1.4` direkt → genau diese Zeile ändern (Text-Ersetzung, **kein** YAML-Roundtrip; Kommentare und Formatierung bleiben).
   - `image: repo:${PLAUSIBLE_VERSION}` → den Wert in der `.env` (Label `environment_file`) ändern, aber nur wenn die Variable ausschließlich für dieses Image genutzt wird; sonst Abbruch mit Erklärung.
   - `extends`, YAML-Anker, mehrere Treffer → Abbruch mit Erklärung (manuell).
4. **Sicherung:** Compose-Datei bzw. `.env` byte-genau nach `/var/backups/image-updates/compose/<projekt>/<stamp>/`; danach `docker compose config` als Validierung.
5. Danach derselbe Ablauf wie Stufe 1 (Dump → Rollback-Tag → `pull` → `up -d --no-deps`).
6. **Rollback** stellt die Compose-Datei byte-genau wieder her und setzt das alte Image zurück.
7. Ist das Projektverzeichnis ein **Git-Repository**: Status anzeigen und optional mit vorgeschlagener Commit-Nachricht committen (nie pushen).

**Sperren**
- Datenbank-Majors: nur Info, siehe Stufe 5.
- Majors allgemein: zusätzliche Bestätigung und Release-Notes-Hinweis.
- Nextcloud: nur ein Major pro Schritt.

**Fertig, wenn …**
- E2E im Wegwerf-Projekt grün ist, für: direkter Tag, `.env`-Variable, Override-Datei, Rollback (Datei byte-identisch), abgelehnte Fälle (Anker, mehrfach genutzte Variable).
- Die Diff-Vorschau genau der späteren Änderung entspricht.

---

## Stufe 3 – Release Notes im Dialog

**Ziel:** Vor dem Klick sehen, was sich ändert.

- **Quelle zum Image finden:** Label `org.opencontainers.image.source`, Hub-Link aus Diun, bekannte Zuordnungen.
- **GitHub-Releases holen** zwischen installierter und Ziel-Version. Das macht der Radar auf dem Server, nicht der Browser. Ergebnis zwischenspeichern und das Rate-Limit beachten: ohne Token 60 Anfragen pro Stunde, optional ein Token in `/etc/image-updates.conf`.
- **Anzeige:** Im Update-Dialog als einklappbare Liste, nur Text, keine externen Bilder. Stichwörter wie „breaking“, „migration“, „deprecated“ werden hervorgehoben.
- **Fertig, wenn** für die Images auf dem Referenz-Host die Notes zwischen zwei Versionen erscheinen. Fehlen sie, steht ein klarer Hinweis statt eines Fehlers.

---

## Stufe 4 – Automatische Updates nach Regeln

**Ziel:** Patch-Updates nachts ohne Klick, mit Sicherheitsnetz.

**Regeln je Container**
- Als Compose-Label (`image-updates.policy=patch|minor|off`) oder im Addon gespeichert in `/etc/image-updates.d/`.
- Standard `off`; Datenbank-Container immer `off`.

**Wartungsfenster:** systemd-Timer, z. B. 03:30. Pro Lauf höchstens N Projekte, nacheinander.

**Sicherheitsnetz**
- Dump und Rollback-Tag wie in Stufe 1.
- Health-Check, danach automatischer Rollback bei `failed`.
- Neue Verben nur für den Timer, nicht aus dem Browser.

**Benachrichtigungen:** Zusammenfassung per E-Mail (lokaler MTA), ntfy oder Matrix-Webhook. Inhalt: was aktualisiert wurde, was fehlschlug, was zurückgerollt wurde.

**Fertig, wenn …**
- Ein Trockenlauf-Modus den Plan zeigt.
- Ein E2E einen absichtlich kaputten Patch automatisch zurückrollt und meldet.

---

## Stufe 5 – Datenbank-Majors begleitet

**Ziel:** Postgres 16 → 18 (und MariaDB-Majors) geführt statt von Hand.

**Weg A, Dump/Restore:**
1. `pg_dumpall` mit der alten Version
2. neuer Container mit neuer Version auf **neuem** Volume
3. Restore
4. Prüfung (Tabellen- und Zeilenzahlen)
5. Umschalten (Compose-Änderung wie Stufe 2)
6. altes Volume bleibt bis zur Freigabe

**Weg B, `pg_upgrade`:** über ein Upgrade-Image (z. B. `tianon/postgres-upgrade`), schneller, aber komplexer.

**Bedienung:** Hohe Hürde – eigener Assistent, Ausfallzeit wird angekündigt, Häkchen „Backup außerhalb geprüft“.

**Fertig, wenn** ein E2E mit Testdaten 16 → 17 per Weg A durchläuft und der Rückweg (altes Volume) dokumentiert und getestet ist.

---

## Stufe 6 – Verteilung und Reichweite

- **Debian-Paket** (`.deb`) mit Plugin, Helfern und Units; GitHub-Releases. Danach ersetzt das Paket `install.sh` für Endnutzer.
- **Private Registries:** Zugangsdaten über eine eigene, root-only Datei, die der Radar trotz Härtung lesen darf (`LoadCredential=` oder `ReadOnlyPaths=`); `skopeo --authfile`.
- **Podman** (`podman compose`, Quadlets) als zweite Laufzeit.
- **Mehrere Hosts:** Cockpit kann zwischen Hosts wechseln, das Plugin läuft je Host. Optional eine Sammelansicht.
- **Lizenz** festlegen (Vorschlag: LGPL-2.1-or-later wie Cockpit, oder MIT).

---

## Technische Schulden / Aufräumen

| Thema | Vorschlag |
|---|---|
| `plugin/diun.js` hat ~1 300 Zeilen in einer Datei | in ES-Module aufteilen (`<script type="module">` funktioniert unter Cockpits CSP ohne Build-Schritt): `i18n.js`, `dialog.js`, `updates.js`, `backups.js` |
| Paketname `diun` (URL `/cockpit/@host/diun`) passt nicht mehr zum Umfang | umbenennen in `image-updates`, `install.sh` entfernt das alte Verzeichnis; Lesezeichen ändern sich |
| Units als Heredoc in `diun-deps.sh` | siehe 1.1 #8 |
| Kein Typ-/Lint-Check | `pyflakes`/`ruff`, `shellcheck` in CI (1.1 #7) |
| `diun-update.py` nur per E2E getestet | Offline-Tests (1.1 #6) |
| Radar-Lauf fest um `:17` alle 6 h | `RandomizedDelaySec=` gegen Registry-Spitzen |
| Dokumentation der JSON-Formate | bei jeder Formatänderung `docs/INTERNALS.md` und die Fixtures in `test/fixtures/` mitziehen |

---

## Offene Entscheidungen

1. Lizenz (siehe Stufe 6).
2. Paket umbenennen (`diun` → `image-updates`)? Wenn ja, früh, solange es wenige Installationen gibt.
3. Sollen automatische Updates (Stufe 4) überhaupt kommen, oder bleibt jedes Update ein bewusster Klick?
