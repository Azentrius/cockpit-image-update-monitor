# Roadmap

As of 2026-10-12. Each stage is cut so that it is useful on its own and brings its own tests. The order
is the recommendation.

**Guardrails for every stage** (they keep applying unchanged):

- **Place:** root code only in `/usr/local/lib/diun-radar`, fixed verbs only, arguments checked by regex.
- **Show first:** every changing action shows a plan first, then the live output, then the result.
- **Stop before the change:** anything that can go wrong stops before something changes (dump, space,
  validation).
- **Language:** English and German for every text, phone layout, no external resources in the browser.
- **Tests:** offline in `test/`, end-to-end only in throw-away projects, never on production containers.

---

## Stage 1 – Update with the same tag ✅

Done (see [CHANGELOG](CHANGELOG.md)):
- Update/roll-out with plan, database dump (Postgres, MariaDB/MySQL), rollback tag, waiting for health,
  rollback
- Backups & dumps tab with delete
- Columns Configured / Installed / Newest version

---

## Stage 1.1 – Polish (small, next)

| # | Topic | Scope | Done when … |
|---|---|---|---|
| 1 | **Start the radar after an update** | After a successful update/rollback `diun-update.py` runs `systemctl start --no-block diun-radar.service` (only if the unit exists). The plugin reloads as soon as `versions.json` changes; the watch is already there. | after updating e.g. ollama (`latest`) the *Installed* column shows the new version within ~1 min instead of `?` |
| 2 | **“Check radar now”** | Button next to *Refresh*; a new fixed verb (e.g. `diun-update.py radar`) that starts the service. Shows “running …” until `versions.json` is new. | the button starts exactly one run; a double click does not start a second one (systemd oneshot) |
| 3 | **Cleanup rules** | Default: keep the last **3** dumps per container, delete unused rollback images after **14 days**. Configurable in `/etc/image-updates.conf`, root-owned, a plain `KEY=VALUE` file. Runs after every successful update and daily by timer. In the backups tab: a “would be deleted” preview and a “Clean up now” button. | tests with a fake file system; a dump of the **last** update or an image in use is never deleted |
| 4 | **Versions in the history** | `operations.jsonl` gets `from_version` / `to_version` (via `diunlib.local_version` or the radar cache). The history shows “16.14 → 16.15”. | old entries without the fields are still shown |
| 5 | **MariaDB/MySQL end-to-end** | Extend `test/e2e-server.sh` with a throw-away project using `mariadb:11` (and `mysql:8`): the dump contains test data, abort on a wrong password. | E2E green for Postgres, MariaDB, MySQL |
| 6 | **Offline tests for `diun-update.py`** | Fake `docker`/`compose` like `test/inv_test.py`: plan reasons, database detection (`kind_of`), exit codes, path checks. | `run-all.sh` covers `diun-update.py` without Docker |
| 7 | **CI** | The `test` workflow runs `test/run-all.sh` and the installer on every push and PR (done). Still missing: `shellcheck lib/*.sh install.sh test/*.sh tools/*.sh`, `python -m pyflakes lib`. | green check on every push/PR, including lint |
| 8 | **systemd units as files** | `diun-radar.service/.timer` live as a heredoc in `diun-deps.sh` today. Move them to `systemd/`; `install.sh` and `diun-deps.sh` use the same source. | one source of truth; `diun-deps.sh install radar` works unchanged |

---

## Stage 2 – Version jumps (change the tag in the compose file)

**Goal:** a click on an *outlined* chip (e.g. “Patch v2.1.5” with `v2.1.4` configured) switches to
that tag safely.

**Flow**

1. Dialog “Switch to `v2.1.5`?” with a **diff preview** of the compose change, a plan as in stage 1 and
   a pointer to the release notes (with content from stage 3 on).
2. New verb `diun-update.py retag <container> <target-tag>`. The target is checked against the
   registry's tag list and the scheme of the installed version.
3. **Where the tag is** (compose label `config_files`, possibly several files/overrides):
   - `image: repo:v2.1.4` directly → change exactly this line (text replacement, **no** YAML round
     trip; comments and formatting stay).
   - `image: repo:${PLAUSIBLE_VERSION}` → change the value in the `.env` (label `environment_file`),
     but only if the variable is used for this image alone; otherwise stop with an explanation.
   - `extends`, YAML anchors, several matches → stop with an explanation (manual).
4. **Backup:** compose file or `.env` byte-exact to
   `/var/backups/image-updates/compose/<project>/<stamp>/`; then `docker compose config` as validation.
5. Then the same flow as stage 1 (dump → rollback tag → `pull` → `up -d --no-deps`).
6. **Rollback** restores the compose file byte-exact and points back to the old image.
7. If the project directory is a **git repository**: show its status and optionally commit with a
   suggested message (never push).

**Blocks**
- Database majors: info only, see stage 5.
- Majors in general: an extra confirmation and a pointer to the release notes.
- Nextcloud: one major per step only.

**Done when …**
- E2E in a throw-away project is green for: direct tag, `.env` variable, override file, rollback (file
  byte-identical), refused cases (anchor, variable used more than once).
- The diff preview matches the later change exactly.

---

## Stage 3 – Release notes in the dialog

**Goal:** see what changes before the click.

- **Find the source of an image:** label `org.opencontainers.image.source`, hub link from Diun, known
  mappings.
- **Fetch GitHub releases** between the installed and the target version. The radar does this on the
  server, not the browser. Cache the result and respect the rate limit: 60 requests per hour without a
  token, optionally a token in `/etc/image-updates.conf`.
- **Display:** in the update dialog as a collapsible list, text only, no external images. Words like
  “breaking”, “migration”, “deprecated” are highlighted.
- **Done when** the notes between two versions appear for the images on the reference host. If they are
  missing, a clear note appears instead of an error.

---

## Stage 4 – Automatic updates by rules

**Goal:** patch updates at night without a click, with a safety net.

**Rules per container**
- As a compose label (`image-updates.policy=patch|minor|off`) or stored by the add-on in
  `/etc/image-updates.d/`.
- Default `off`; database containers always `off`.

**Maintenance window:** systemd timer, e.g. 03:30. At most N projects per run, one after another.

**Safety net**
- Dump and rollback tag as in stage 1.
- Health check, then automatic rollback on `failed`.
- New verbs only for the timer, not from the browser.

**Notifications:** a summary by email (local MTA), ntfy or Matrix webhook. Content: what was updated,
what failed, what was rolled back.

**Done when …**
- A dry-run mode shows the plan.
- An E2E rolls back a deliberately broken patch automatically and reports it.

---

## Stage 5 – Guided database majors

**Goal:** Postgres 16 → 18 (and MariaDB majors) guided instead of by hand.

**Path A, dump/restore:**
1. `pg_dumpall` with the old version
2. new container with the new version on a **new** volume
3. restore
4. check (table and row counts)
5. switch over (compose change as in stage 2)
6. the old volume stays until released

**Path B, `pg_upgrade`:** through an upgrade image (e.g. `tianon/postgres-upgrade`), faster but more
complex.

**Use:** a high bar – its own assistant, downtime is announced, a “backup checked elsewhere” tick box.

**Done when** an E2E with test data runs 16 → 17 via path A and the way back (old volume) is documented
and tested.

---

## Stage 6 – Distribution and reach

- **Debian package** (`.deb`) with plugin, helpers and units, attached to the GitHub releases (which
  the release workflow makes today as source tarballs). The package then replaces `install.sh` for end
  users.
- **Private registries:** credentials in a separate root-only file that the radar may read despite its
  hardening (`LoadCredential=` or `ReadOnlyPaths=`); `skopeo --authfile`.
- **Podman** (`podman compose`, Quadlets) as a second runtime.
- **Several hosts:** Cockpit can switch between hosts, the plugin runs per host. Optionally a combined
  view.

---

## Technical debt / cleanup

| Topic | Proposal |
|---|---|
| `plugin/diun.js` has ~1,300 lines in one file | split into ES modules (`<script type="module">` works under Cockpit's CSP without a build step): `i18n.js`, `dialog.js`, `updates.js`, `backups.js` |
| Package name `diun` (URL `/cockpit/@host/diun`) no longer fits the scope | rename to `image-updates`, `install.sh` removes the old directory; bookmarks change |
| Units as a heredoc in `diun-deps.sh` | see 1.1 #8 |
| No type/lint check | `pyflakes`/`ruff`, `shellcheck` in CI (1.1 #7) |
| `diun-update.py` only tested by E2E | offline tests (1.1 #6) |
| Radar run fixed at `:17` every 6 h | `RandomizedDelaySec=` against registry peaks |
| Documentation of the JSON formats | update `docs/INTERNALS.md` and the fixtures in `test/fixtures/` with every format change |

---

## Open decisions

1. Rename the package (`diun` → `image-updates`)? If yes, early, while there are few installations.
2. Should automatic updates (stage 4) come at all, or does every update stay a deliberate click?

Decided: the license is Apache-2.0 (see [LICENSE](LICENSE)).
