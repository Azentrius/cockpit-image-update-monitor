# Roadmap

As of 2026-10-13. Each stage is cut so that it is useful on its own and brings its own tests. The order
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

## Stage 1.2 – Know before switching, fail clearly

**Goal:** an update never surprises. The dialog says how big the jump is, or that nobody knows. If the
containers do not come up, the reason is visible at once. A failed build is not offered the same way
again.

Done (see [CHANGELOG](CHANGELOG.md)), marked ✅ below: in 0.8.0 the unknown jump size and the most
likely cause of a failure (“reason first”); in 0.9.0 the kept output, the restart-loop test, the
failed builds, what is updated together and the dependencies; in 0.10.0 the floating tags. Open: the
update in two steps.

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
- **Update in two steps** (a choice in the dialog): fetch first (pull only; nothing that runs changes,
  the row shows “Roll-out pending” with the version that came), roll out second.

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
  says so under the container name, e.g. “together with glitchtip-web” (at most three names, then
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

## Stage 2 – Version jumps (change the tag in the compose file)

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
| `plugin/diun.js` has ~1,550 lines in one file | split into ES modules (`<script type="module">` works under Cockpit's CSP without a build step): `i18n.js`, `dialog.js`, `updates.js`, `backups.js` |
| Package name `diun` (URL `/cockpit/@host/diun`) no longer fits the scope | rename to `image-updates`, `install.sh` removes the old directory; bookmarks change; the units (`diun-radar`, `diun-cleanup`) and `/usr/local/lib/diun-radar` would follow |
| Changed unit templates do not reach existing installations | `install.sh` only reports the difference; an opt-in `diun-deps.sh update units` with a backup |
| Documentation of the JSON formats | update `docs/INTERNALS.md` and the fixtures in `test/fixtures/` with every format change |

---

## Open decisions

1. Rename the package (`diun` → `image-updates`)? If yes, early, while there are few installations.
2. Should automatic updates (stage 4) come at all, or does every update stay a deliberate click?
3. Roll back automatically when the containers do not come up after an update started by hand? Only
   as an option: if the new version has migrated its database already, the old program no longer fits
   the data.

Decided: the license is Apache-2.0 (see [LICENSE](LICENSE)).
