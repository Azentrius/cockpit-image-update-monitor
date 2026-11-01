# Roadmap

As of 2026-11-01 (0.26.1). Each stage is cut so that it is useful on its own and brings its own tests. The order
is the recommendation.

**Guardrails for every stage** (they keep applying unchanged):

- **Place:** root code only in `/usr/lib/cockpit-image-updates`, fixed verbs only, arguments checked by regex.
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

## Stage 1.1 – Polish ✅

Done in 0.7.0 (see [CHANGELOG](CHANGELOG.md)):
- Radar starts after every successful update/rollback; “Run radar now” in the *Running containers*
  card (`diun-update.py radar`, waits for the run, a second click joins it).
- Cleanup rules (`/etc/image-updates.conf`: `KEEP_DUMPS=3`, `ROLLBACK_IMAGE_DAYS=14`) after every
  update for that project, daily via `diun-cleanup.timer` (set up on request), preview and “Clean up
  now” in the backups tab.
- `from_version` / `to_version` in the history.
- End-to-end test for MariaDB and MySQL; offline tests for `diun-update.py`; lint (shellcheck, ruff), the browser
  preview and `systemd-analyze verify` in CI; systemd units as files in `systemd/`.
- The end-to-end test passed on the reference host (Postgres, MariaDB, MySQL).
- In the same release, though not part of the stage: the shared Azentrius page frame (header, tabs,
  cards, status labels, signature) with the design files of the Cron Jobs add-on.

Decided on the way:
- The cleanup after an update stays inside that project and image; only the timer and “Clean up now”
  work on everything.
- The daily timer is optional and set up from the backups tab, not from the requirements panel.
- Rules that are not understood stop the cleanup; there is no fallback to the defaults.

---

## Stage 1.2 – Know before switching, fail clearly ✅

**Goal:** an update never surprises. The dialog says how big the jump is, or that nobody knows. If the
containers do not come up, the reason is visible at once. A failed build is not offered the same way
again.

Done (see [CHANGELOG](CHANGELOG.md)), marked ✅ below: in 0.8.0 the unknown jump size and the most
likely cause of a failure (“reason first”); in 0.9.0 the kept output, the restart-loop test, the
failed builds, what is updated together and the dependencies; in 0.10.0 the floating tags; in 0.23.0 the
update in two steps; in 0.24.0 the rebuild of images built on the server.

**Seen on the reference host:** `latest` of an app moved to a new major version that needs an extra
service; the app did not start. Dump, rollback copy and rollback worked, and the data stayed untouched.
But the add-on could not warn beforehand: the image has no version label and no version variable, so the
installed version was unknown and the jump invisible.

**Before switching**
- ✅ **Unknown jump size:** if the installed version is unknown and the tag fixes no version number
  (`latest`, `stable`; reach 0 in the README's terms), the plan says “the size of the jump is unknown; a
  new major version is possible” and asks for an extra confirmation.
- ✅ **Resolve floating tags:** the radar finds the version tag with the same digest as the configured
  tag in the registry (e.g. `latest` = `6.0.2`), without a pull. For the installed image it searches
  further back than the 20 newest tags, once, and keeps the result. A jump such as 5.x → 6.0.2 then
  shows as a major chip; the update dialog names the target and waits for a tick box on a new major
  version.
- ✅ **Update in two steps** (a choice in the dialog): fetch first (pull only; nothing that runs changes,
  the row shows “Roll-out pending” with the version that came), roll out second.
- ✅ **Images built on the server** (0.24.0): the version their final base brings as the installed
  one, and *Rebuild* once a base is newer (`docker compose build --pull` in place of the pull, with
  dumps, rollback copy and history).

**While switching**
- ✅ **Reason first:** when containers do not come up, the result names the most telling error line
  (e.g. “Invalid environment variables: …”) above the log lines, next to the rollback button. It is
  also kept in the history.
- ✅ **Keep the output:** the live output of every update and rollback is stored with the history
  (`/var/backups/image-updates/logs/<op-id>.log`, root only) and can be opened from the history.
- ✅ **Restart loops:** `wait_healthy` already counts a restarting container as failed. An end-to-end case
  with an app that exits at once under `restart: always` proves that the update fails within seconds,
  not after `HEALTH_TIMEOUT`.

**After a failure**
- ✅ **Remember failed builds:** the registry digests of the new build are stored with the operation.
  When Diun reports exactly that build again, the row says “The update to this build failed on <date>”,
  and the dialog repeats the cause and asks for a tick box.
- Pinning the tag to the running version comes with stage 2 (change the tag in the compose file).

**What is updated together** ✅
- **Overview:** if other running containers of the same compose project use the same image, the row
  says so under the container name, e.g. “together with bugs-web” (at most three names, then
  “+n”). Visible text, not a tooltip, so it works on phones. The inventory knows project and image of
  every container already; no new helper verb is needed.
- **Dialog:** “Affected containers” splits into groups: recreated (same image), one-off jobs that run
  first (e.g. `migrate`), databases that are dumped, and, with the dependencies below, the services
  that are watched.
- Rows stay one per container: the status belongs to each container, and a combined row would hide
  the one that is not healthy.

**Dependencies** ✅ (from `docker compose config`, which the plan reads already)
- **Show them:** the plan lists what the services depend on (`depends_on`) and what depends on them.
  The update recreates only the services of the image (`up --no-deps`): a database or cache restarts
  under the running app, which has to reconnect by itself.
- **Watch them:** after such an update the add-on also watches the dependent services for the waiting
  time (still running, no new restarts, healthy if they have a health check) without restarting them. A
  failure there makes the update fail and offers the rollback.
- **Migration jobs:** a one-off job (e.g. `migrate`) with the same image as long-running services that
  do not wait for it (`depends_on: … condition: service_completed_successfully`): the plan warns that the
  services can start before the migration is done. Seen with an app whose worker briefly hit a table the
  migration had not created yet.
- **Other projects** on the same user-defined network (e.g. one mail relay for several projects) are
  shown as a hint. Being on a network does not prove use, so this never blocks anything.

**Done when …**
- Offline tests cover the plan hint, the error line, the stored output, the remembered digest, the
  “together with” hint, the groups in the dialog and the dependency lists; fixtures for floating tags
  with and without a known version.
- End to end: an image that exits at once fails within seconds and rolls back with one click; the output
  of the run is stored; a dependent service is watched after a database update.
- On the reference host `latest` is resolved for every image whose registry the radar can read.

---

## Stage 2 – Version jumps (change the tag in the compose file) ✅

Done in 0.11.0 (see [CHANGELOG](CHANGELOG.md)): the radar's choices per installed version,
`retag --dry-run` (the lines before and after, plan, fingerprint), `retag --expect` (the change with
byte-exact copies, rollback restoring them) and *Change version* in the page (row button, outlined
chips, selection field, preview, tick boxes). Where it differs from the plan below:
- Several lines of one image (e.g. `web`, `worker`, `migrate`) change together, as an update recreates
  all of them; only anchors, `include:` and shared or composed variables stop it.
- A git repository is named as a hint; the add-on does not commit.
- The pointer to the release notes came with stage 3 (0.23.0).
- Database majors can be chosen since stage 5 for PostgreSQL (0.23.0) and MariaDB (0.25.0); they
  run as a database major, not as a tag change. Other database majors stay locked.

**Goal:** the version of a container can be changed in the page, safely: a click on an *outlined* chip
(e.g. “Patch v2.1.5” with `v2.1.4` configured), or a selection field with the versions the registry
offers.

**Choosing the target** (a selection field in the dialog, filled from the radar's tag list)
- The configured tag first, marked as such.
- “Pin to the running version”: the exact tag of what runs now (needs the version known, see stage 1.2
  “resolve floating tags”).
- Version lines the registry offers as floating tags, e.g. `5` or `5.4`: they keep patch and minor
  updates coming without the next major.
- Exact versions of the same scheme, newest first, grouped by patch / minor / major relative to the
  installed version.
- `latest` (or the image's other floating tags), with the warning that it follows every release.
- Database majors are shown but cannot be chosen (stage 5). Other majors need an extra confirmation.

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

## Stage 3 – Release notes in the dialog ✅

Done in 0.23.0 (see [CHANGELOG](CHANGELOG.md)): the radar names each image's repository on GitHub
(`source` in `versions.json`), fetches the releases of the images with something newer into
`releases.json`, and the update dialog and the preview of a version change show those between the
installed and the target version. Where it differs from the plan below:
- The source: a short list of checked images, the labels `org.opencontainers.image.source` and
  `org.label-schema.vcs-url`, and `ghcr.io/<owner>/<name>`. Diun's hub link names Docker Hub's page,
  not the source, and is not used.
- No token yet: without a login, with one request per image that has something newer, an answer kept
  for 24 hours and at most 20 requests per run, the 60 per hour are enough on the reference host. A
  token in `/etc/image-updates.conf` stays open (the cleanup's strict parser has to learn the key, and
  the radar must read it despite its hardening without the page ever seeing it).
- The pointer to the release notes in a version change (stage 2) is these notes.

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

## Stage 3.1 – What the update round of 2026-11-01 showed

**Seen on the reference hosts:** about 30 updates from the page, among them five PostgreSQL majors
(15 → 17 and 16 → 17, volumes and a bind mount), tag changes to line tags and one failed major of an
app with its rollback. Everything worked as designed: the trial runs, the counts, the login checks and
the rollback, which put back the compose file byte for byte and the old image ID. These points are where
the add-on says too little or protects too little.

- **SQLite files are not saved.** ntfy (its user database, schema 9 → 10) and the Nextcloud MCP server
  (Alembic migrations) migrated their SQLite files on the first start of the new image. A rollback then
  puts the old image on a migrated file. Proposal: the plan finds SQLite files in the mounts of the
  services (by their file header, with a size limit), the update copies them next to the dumps while the
  containers are stopped, and the rollback puts them back. At least the dialog names them and warns.
- **A major of an app is noticed only when it fails.** *Change version* offered Formbricks 5.4 → 6;
  version 6 refuses to start without SpiceDB (`AUTHZED_*`). The restart loop was caught, the reason taken
  from the log and the rollback worked; the data stayed untouched only because version 6 checks its
  environment before its migrations. Proposal: a tag change to a newer major of an app gets a warning box
  as a database major does, with the release notes marked “breaking” at the top and a tick box.
- **The new version stays empty after an update.** For images without a version label (docuseal,
  ollama, ntfy, sharewerk, cube, Forgejo, the Nextcloud MCP server) `to_version` is `null` until the
  next radar run fills it in. Proposal: look up the new digest at once (the radar's digest cache or one
  tag listing), or at least say “filled in by the next radar run” instead of an empty cell.
- **A skipped login check is not said.** A PostgreSQL major without `POSTGRES_PASSWORD` in the
  container's environment (Glitchtip) skips the login check after the restore silently. Proposal: say
  so in the output and in the history (“login check skipped: no `POSTGRES_PASSWORD`”).
- **“Configuration changed” without the difference.** On one host two projects (`forgejo-runner`,
  `pomerium`) show `config_changed`, although their compose files are older than their containers and
  Docker was not updated; the cause is unknown, likely variables of the shell that created them. An
  update would roll out a configuration nobody has seen. Proposal: the plan names what would change
  between `docker compose config` and the running container: image, command, volumes, ports, networks
  and the names (never the values) of environment variables.
- **Expected error lines after a database major.** After every PostgreSQL major the new server logs
  `ERROR: role "<user>" already exists`: the dump creates the role again that the image created on its
  first start, and the helper ignores exactly this line. Proposal: say so in the output after the
  restore and in the README, so that a look into the database log does not alarm.

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

## Stage 5 – Guided database majors (PostgreSQL ✅, MariaDB ✅)

PostgreSQL done in 0.23.0, MariaDB in 0.25.0 (see [CHANGELOG](CHANGELOG.md)). The helper does a
major by dump and restore (`diun-update.py dbmajor`, see
[docs/INTERNALS.md](docs/INTERNALS.md#dbmajor-database-major-stage-5-since-0230)), with its rollback
and the release of the kept data; the end-to-end test runs PostgreSQL 16 → 17 and MariaDB 11 → 12
with test data, the rollback and the release. The page offers it in “Change version” with two tick
boxes (a backup checked elsewhere, the downtime) and releases the kept data from the history.
- 0.24.1: every move of the data is in the history before it starts, so a run stopped by a signal or
  a kill can be rolled back, and the rollback starts the old major only on its complete data.
- 0.25.0: a trial run in a throwaway container of the new major comes before the data changes; the
  plan refuses PostgreSQL roles with an MD5 password, which the new major would lock out; the user of
  the environment must log in after the restore. MariaDB runs the same way, with `mariadb-upgrade`
  after the restore, and dumps the users' databases and users, not the old system tables.

Open: MySQL majors, path B (`pg_upgrade`), pgvector's `pg17` tags (the radar has no version tags to
compare there).

Where it differs from the plan below:
- No new volume and no change of the volume line: the old data is moved aside (into a volume of its
  own next to a named volume, into a sibling directory of a bind mount), by renames on the same
  filesystem, and the new major starts on the emptied place. The compose file changes in its tag line
  only (stage 2). The old data stays until released; a rollback moves it back.
- The services that wait for the database are stopped from the dump on, so that nothing is written
  that the dump lacks; the extensions in use are checked in the new image before anything changes.
- From 18 on the image keeps its data in `/var/lib/postgresql/18/docker`: without `PGDATA` in the
  compose file (or the volume at `/var/lib/postgresql`) the plan refuses and says what to change.

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

Packages and Cockpit's Applications page are done in 0.12.0 (see [CHANGELOG](CHANGELOG.md)), marked ✅.

- ✅ **Packages** (`.deb` and `.rpm`, named `cockpit-image-update-monitor` since 0.13.0, before that
  `cockpit-image-updates`) with plugin, helpers and units, attached to the GitHub releases; they
  replace `install.sh` and take over its installations.
- ✅ **Cockpit's Applications page:** AppStream data (`de.azentrius.image_updates`) in the packages, and
  the Azentrius package repository, packages.azentrius.com, whose catalog Cockpit reads: the add-on is
  found and installed from Cockpit itself.
- **Private registries:** credentials in a separate root-only file that the radar may read despite its
  hardening (`LoadCredential=` or `ReadOnlyPaths=`); `skopeo --authfile`.
- **Podman** (`podman compose`, Quadlets) as a second runtime.
- **Several hosts:** Cockpit can switch between hosts, the plugin runs per host. Optionally a combined
  view.

---

## Technical debt / cleanup

| Topic | Proposal |
|---|---|
| ✅ `plugin/diun.js` had ~2,000 lines in one file | done in 0.23.0: `common.js`, `dialog.js`, `notes.js`, `update.js`, `retag.js`, `overview.js`, `backups.js` and `diun.js` (texts in `i18n.js` since 0.19.0). Classic scripts sharing `window.ImageUpdatesPage`, not ES modules: the jsdom tests run the page's scripts as the browser does, and jsdom runs no modules |
| Documentation of the JSON formats | update `docs/INTERNALS.md` and the fixtures in `test/fixtures/` with every format change |

---

## Open decisions

1. Should automatic updates (stage 4) come at all, or does every update stay a deliberate click?
2. Roll back automatically when the containers do not come up after an update started by hand? Only
   as an option: if the new version has migrated its database already, the old program no longer fits
   the data.

Decided: the license is Apache-2.0 (see [LICENSE](LICENSE)).
