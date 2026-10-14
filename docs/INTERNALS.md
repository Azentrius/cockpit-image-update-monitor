# Internals: interfaces, formats, flows

Reference for everyone working on the code. The helpers are the **only** bridge between the browser and
root. Whoever changes a format changes helper, plugin, fixtures (`test/fixtures/`) and this document
together.

## Paths

| Path | Content | Owner / mode |
|---|---|---|
| `/usr/share/cockpit/diun/` | plugin | root, 644 |
| `/usr/local/lib/diun-radar/` | helpers (`*.py`, `*.sh` 755, `diunlib.py` 644), backups made by `install.sh` (`backup-*`, 700) | root |
| `/usr/local/lib/diun-radar/systemd/` | unit templates from `systemd/`, copied by `diun-deps.sh install radar\|cleanup` | root, 644 |
| `/var/lib/diun-radar/versions.json` | result of the version radar | root, 644 (StateDirectory) |
| `/var/lib/diun/events.jsonl` | Diun events (written by the notifier **inside the container**) | writable from the Diun container → data only, never code |
| `/var/lib/diun/dismissed.json` | Diun events the plugin marked as dismissed | data |
| `/var/backups/image-updates/` | `dumps/<project>/*.sql.gz`, `operations.jsonl`, `logs/<op-id>.log` and `.rollback.log` | root, 700 / 600 |
| `/etc/image-updates.conf` | cleanup rules (optional, see below) | root, not writable by others |
| `/opt/diun/` | Diun stack (`docker-compose.yml`, `diun.yml`), from `diun-deps.sh install diun` | root |
| `/etc/systemd/system/diun-radar.{service,timer}` | radar every 6 h (`*-*-* 00/6:17:00`) | root |
| `/etc/systemd/system/diun-cleanup.{service,timer}` | cleanup daily (`04:40` + up to 20 min) | root |
| `/run/lock/diun-{inventory,update,deps}.lock` | `flock` locks (`diun-update.lock` also for `cleanup`) | root |

## Calls from the plugin

All through `cockpit.spawn(argv, { superuser, err, environ: ["DIUN_LANG=de|en"] })`:
- Reading (inventory, check, plan, backups): `superuser: "try"`, `err: "message"`.
- Actions: `superuser: "require"`, `err: "out"`, the output is streamed live.

| Helper | Call | Kind | Output |
|---|---|---|---|
| `diun-inventory.py` | (no arguments) | read | JSON lines, see below |
| `diun-deps.sh` | `check` | read | one JSON object |
| | `install skopeo` / `install diun` / `install radar` / `install cleanup` / `start diun` | action | text, two languages |
| `diun-update.py` | `plan <container>` | read | one JSON object |
| | `update <container> [--no-dump]` | action | text + last line `@@RESULT {…}` |
| | `rollback <op-id>` | action | text + `@@RESULT {…}` |
| | `backups` | read | one JSON object |
| | `log <op-id>` | read | text: the kept output of the update, then `--- Rollback ---` and that of its rollback; exit 2 if none |
| | `delete-dump <project>/<file>.sql.gz` | action | text |
| | `delete-image <repo>:rollback-YYYYMMDD-HHMMSS` | action | text (`docker rmi`, without `-f`) |
| | `cleanup` | action (also `diun-cleanup.service`) | text + `@@RESULT {…}` |
| | `cleanup --dry-run` | read (console) | text |
| | `radar` | action, no dialog | text; waits for the radar run (`systemctl start diun-radar.service`) |
| `diun-versions.py` | `--output FILE` (systemd), `--table`, without arguments JSON lines | timer / diagnosis | – |

Inputs are checked by regex:

| Input | Regex |
|---|---|
| container name | `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$` |
| operation ID | `^\d{8}-\d{6}-<name>$` |
| dump path | `^<name>/\d{8}-\d{6}_<name>\.sql\.gz$` + `realpath` check |
| rollback tag | `^rollback-\d{8}-\d{6}$` |

`DIUN_LANG` only selects the language of the text output.

### Exit codes (all helpers)

| Code | Meaning |
|---|---|
| 0 | ok (also `nochange`; `cleanup` also when single items could not be removed, see `failed`) |
| 1 | unexpected error of a subcommand (stderr in the output; `radar`: the run failed) |
| 2 | not found / not possible (container, operation, rollback image gone, not updatable, radar not set up, cleanup rules not understood or not root-only) |
| 3 | refused (already rolled back; file/container exists on `install`) |
| 4 | after update/rollback the containers do not run stably |
| 5 | stopped before any change (dump failed, too little space, database not running) |
| 64 | wrong call |
| 75 | lock taken (already running) |
| 77 | no root rights |

## Formats

### Inventory – one line per running container

```json
{"name": "glitchtip-postgres", "image": "postgres", "tag": "16-alpine",
 "digest": "sha256:16bc…", "imgid": "sha256:16bc…",
 "version": "16.14", "version_src": "env:PG_VERSION",
 "tagid": "sha256:7218…", "pulled": true, "pulled_version": "16.15",
 "project": "glitchtip", "service": "glitchtip-postgres", "managed": true}
```

- `image` is the repo without the tag, as written in the compose file.
- `version_src` is `tag`, `env:<NAME>` or `null`. Only the radar knows the label and digest sources.
- `pulled` means: the local tag points to a different image than the running one.
- `managed` means: the compose labels for project, service, working directory and files are present.
- With Docker's containerd image store `imgid` equals the index digest. That is normal.

### `versions.json` (radar)

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
               "status": "major", "as_tag": "16.14-alpine", "scheme": "N.N-alpine",
               "choices": {
                 "pin": "16.14-alpine",
                 "lines": [{"tag": "18-alpine", "newest": "18.6-alpine"},
                           {"tag": "16-alpine", "newest": "16.15-alpine"}],
                 "newer": [{"tag": "16.15-alpine", "level": "minor"},
                           {"tag": "18.6-alpine", "level": "major"}],
                 "floating": ["alpine"]}}
   },
   "by_tag": {"patch": null, "minor": null, "major": "18-alpine", "status": "major"},
   "newest": "18.6",
   "tag_now": {"digest": "sha256:…", "version": "16.15", "tag": "16.15-alpine"},
   "status": "major",
   "checked": "…",
   "stale": true, "error": "…"
 }]}
```

| Field | Meaning |
|---|---|
| `installed` | per **image ID**, also for the newer image that is already pulled. `src` is `tag`, `env:<NAME>`, `label` or `digest`. Digest matches are the cache for the next run: hits are kept for good, `version: null` is retried after 7 days. |
| `versions[<installed version>]` | newer tags in the scheme of the installed version. `status: "unknown"` when there is no matching scheme (e.g. pgvector `pg17` with `PG_VERSION` 17.10). |
| `versions[…].choices` | (since 0.11.0, for the stage-2 selection field) what can replace the configured tag, in the installed version's scheme and never older: `pin` = the installed version's own tag, `null` if the registry lacks it; `lines` = major and minor lines (`5`, `5.4`, `16-alpine`; one or two blocks, never more) and the newest version tag in each, broad lines first, at most 12: only lines with a released version in them (a bare `9.2` before `9.2.0` may be a pre-release), the running version's own lines always, the others newest first; `newer` = exact tags with `level` `patch` / `minor` / `major`: every newer patch of the installed minor line (at most 10), the newest of each newer minor line and of each newer major (at most 15 each), newest first per level; `floating` = `latest`, `stable`, `production` (with the flavour suffix, e.g. `stable-apache`, or the bare flavour `apache`) when the registry has them. Pre-releases (`-rc.1`), other flavours and `v`-prefixed twins never count. Missing without a scheme and in data from radars before 0.11.0. |
| `by_tag` | comparison with the configured tag itself. Only when the tag contains numbers; the plugin uses it as a fallback. |
| `reach` | levels the configured tag delivers with the Update button. |
| `newest` | highest plain version tag. Shown when the installed version is unknown. |
| `tag_now` | (since 0.10.0) only for floating tags (fewer than two version blocks: `latest`, `16-alpine`, `5`): the digest the tag has in the registry right now (Docker Hub API or `skopeo inspect --raw`, no pull) and the version tag with the same digest (`version` / `tag`, `null` if none). Reused while the digest stays the same; missing when the lookup failed. |
| `status` | `major`, `minor`, `patch`, `current`, `unknown` or `error`, for sorting and for the table. |
| `stale` / `error` | registry error in this run. The rest of the entry comes from the last good run. |

How the plugin computes:

```
installed = inventory.version  ||  versions.installed[imgid].version
compare   = versions.versions[installed]  (if missing or "unknown": by_tag)
newer     = chips patch/minor/major; filled if in reach, otherwise outlined; db+major = grey
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
 "free_bytes": 84654432256, "config_changed": false, "pulled": false, "old_image": "sha256:…",
 "installed_version": "6.1.8", "tag_digits": 0, "jump_unknown": false}
```

- `reason` when not updatable: `not-compose`, `compose-file-missing` (+`missing`), `digest-pinned`,
  `built-locally` or `compose-config-error` (+`detail`).
- `services`: all services of the project with the **same** image reference.
- `dbs`: all database containers of the project.
  - `kind`: `postgres` or `mysql` when the command starts the server; `unsupported` for
    redis/valkey/mongo/clickhouse … (no dump).
  - `size`: Postgres only, sum of `pg_database_size`.
- `config_changed`: `docker compose config --hash` differs from the label
  `com.docker.compose.config-hash`. The change is rolled out as well.
- `installed_version` (since 0.8.0): the version of the running image, as in the history
  (`from_version`, see below); `null` if unknown.
- `tag_digits`: how many version blocks the configured tag fixes (`diunlib.tag_digits`): `latest` 0,
  `16-alpine` 1, `3.12-slim` 2, `v2.1.4` 3.
- `jump_unknown`: `installed_version` is `null` **and** `tag_digits` is 0. Nobody can tell how big the
  jump is; the plugin warns first and enables *Update* only after a tick box.
- `target_version` (since 0.10.0): what the update brings: the version of the image pulled already, or
  `tag_now.version` from the radar (the installed version when `tag_now.digest` is the running image's).
  `tag_now.version` counts only when the installed version is unknown or the radar found it among the
  tags (`versions[<installed>].status` is not `unknown`, `same_series`): `pgvector/pgvector:pg17` runs
  PostgreSQL 17.10 (`PG_VERSION`) while its tags count pgvector releases (`0.8.7-pg17`). `null` if
  unknown.
- `jump`: `major`, `minor` or `patch` by the first version block that differs, `same`, or `null` when
  either version is unknown. On `major` the plugin warns and waits for a tick box.
- Relations (since 0.9.0, from `docker compose config` and the containers of the project):
  - `jobs`: containers among `containers` that run once (not running, restart policy `no` or
    `on-failure`), e.g. migrations.
  - `depends_on`: `[{"service", "condition"}]`, the services outside the image's that they wait for.
  - `dependents`: `[{"service", "containers"}]`, services whose `depends_on` names one of the image's.
    `update` watches those that run (see below).
  - `job_order`: `[{"job", "services"}]`, services of the same image that do not wait for the job with
    `condition: service_completed_successfully`.
  - `shared_networks`: `[{"network", "projects"}]`, other compose projects (or container names
    without a project) on the user-defined networks of these containers, except `<project>_default`.

### `@@RESULT` (last line of `update` / `rollback` / `cleanup`)

```
update:   @@RESULT {"op": "20261012-015025-imgupd-test-db", "status": "ok|nochange|failed",
                    "rollback": "repo:rollback-20261012-015025", "dumps": ["project/…sql.gz"],
                    "from_version": "16.14", "to_version": "16.15"}
          (on failed also "reason": {…}, see below)
rollback: @@RESULT {"op": "…", "status": "ok|failed"}            (on failed also "reason")
cleanup:  @@RESULT {"status": "ok|nochange", "dumps": ["project/…sql.gz"],
                    "images": ["repo:rollback-…"], "bytes": 3145728, "failed": ["…"]}
```

The plugin hides this line in the log and evaluates it. On `failed` (exit 4) it offers the rollback
with `op`. The versions can be `null` (unknown).

`reason` (since 0.8.0), the most likely cause of a failed update or rollback:

```json
{"container": "formbricks-app", "state": "restarting", "exit_code": 1, "oom": false, "source": "log",
 "line": "Error: Invalid environment variables: - AUTHZED_ENABLED: Formbricks v6 requires …"}
```

- `container`: the first failed container with a telling line, otherwise the first failed one;
  `null` when `docker compose up` itself failed (`source: "compose"`, `exit_code` = its exit code).
- `state`: the container state, or the health state `unhealthy` / `starting`.
- `source`: `log` (the last 80 log lines), `health` (last output of the health check, for an
  unhealthy container), `compose` (the last 50 lines `docker compose up` printed) or `null`.
- `line` (`reason_line`): the last line that names an error (`Error:`, `TypeError:`,
  `…Exception`, `error:`, `panic:`, `FATAL`, `PANIC`, `CRITICAL`), plus the next line when it ends in
  `:`; otherwise the last line that mentions a problem (`error`, `failed`, `refused`, `not found` …).
  Stack frames never count, colour codes are removed, at most 300 characters. `null` when nothing fits;
  the plugin then names the state (out of memory, exit code, not ready in time).

### Cleanup rules (`/etc/image-updates.conf`)

```
# Dumps kept per database container (newest first); 0 = never delete dumps
KEEP_DUMPS=3
# Unused rollback images are removed after this many days; 0 = never
ROLLBACK_IMAGE_DAYS=14
```

- Without the file the defaults above apply. `KEY=VALUE` lines, `#` comments, values `0`–`9999`.
- The file must belong to root and must not be writable by group or others. An unknown key or a
  value that is not a number stops the cleanup (exit 2) – nothing is deleted on a guess.
- **Never deleted:** the dumps of each project's last update (`operations.jsonl`), files not named
  `<stamp>_<container>.sql.gz`, rollback images a container (running or stopped) uses. Images are
  removed with `docker rmi` without `-f`.
- The age of a rollback image is the stamp in its tag (time of the update), not the image's build
  date.
- **When:** after every successful `update`, limited to that project's dumps and that image's
  rollback copies; daily by `diun-cleanup.timer` for everything; on “Clean up now”. A cleanup takes
  the update lock (exit 75 while an update runs; the timer counts that as success).

### `operations.jsonl`

One JSON line per operation. Later lines with the same `id` complete earlier ones; this is how a
rollback marks the operation with `rolled_back`, `rollback_ts` and `rollback_status`.

```json
{"id": "20261012-015025-imgupd-test-db", "ts": "…", "kind": "update", "container": "imgupd-test-db",
 "project": "imgupd-test", "workdir": "/root/imgupd-test", "files": ["…/docker-compose.yml"], "env_files": [],
 "services": ["app", "db"], "containers": ["imgupd-test-app", "imgupd-test-db"],
 "image": "imgupd-test/pg:16", "old_id": "sha256:…", "new_id": "sha256:…",
 "from_version": "16.14", "to_version": "16.15",
 "rollback": "imgupd-test/pg:rollback-20261012-015025", "dumps": ["imgupd-test/…sql.gz"], "status": "failed"}
```

- `from_version` / `to_version` (since 0.7.0): the tag or the app's version variable of the old and
  the new image (`diunlib.local_version`), otherwise what the radar found for that image ID
  (label / digest), otherwise `null`. Older entries do not have them; the plugin shows “—”.
- `reason` (since 0.8.0): on a failed update, the same object as in `@@RESULT`. A failed rollback adds
  `rollback_reason` with the line that marks it rolled back. The backups tab shows the cause under the
  result (the rollback's if it failed, otherwise the update's).
- **Dependent services** (since 0.9.0): after the recreated containers are healthy, `update` checks
  the running containers of `dependents` five times, 3 seconds apart, against their state from before
  the update (`StartedAt`, `RestartCount`, not `unhealthy`). One that stopped, restarted or turned
  unhealthy makes the update fail (exit 4) with its own `reason`.
- `new_digests` (since 0.9.0): the registry digests of the new image (`RepoDigests`, `sha256:…`), the
  values Diun reports as `digest`. `[]` for images built or tagged locally.
- **Output:** `update` and `rollback`, called through `main`, keep what they print in
  `logs/<op-id>.log` and `logs/<op-id>.rollback.log` (600, written atomically, at most 512 KiB, without
  `@@RESULT`). Only runs that reach the history keep output; an update stopped before any change does
  not. The files stay with their history entry; nothing deletes them.

`backups` returns the last 30 operations, with `can_rollback`, `log` and `rollback_log` (whether output
is kept) added and without paths and image IDs.
On top come `dumps[]` (`path`, `project`, `container`, `size`, `mtime`), `images[]` (`ref`, `id`,
`size`, `created`, `in_use`), `dump_bytes`, `free_bytes`, `dir` and `cleanup`:

```json
"cleanup": {"keep_dumps": 3, "image_days": 14, "file": "/etc/image-updates.conf",
            "error": null, "dumps": ["glitchtip/20260901-031500_glitchtip-postgres.sql.gz"],
            "images": ["postgres:rollback-20260901-031500"], "bytes": 2936012}
```

`dumps` / `images` / `bytes` are what the rules would delete right now; `file` is `null` with the
defaults. With rules that are not understood, `error` holds the message and the rest is empty. A
plugin without the field (helper older than the plugin) hides the cleanup box.

### `diun-deps.sh check`

```json
{"root": true,
 "docker": {"state": "ok|no-cli|no-daemon", "version": "27.x", "compose": true},
 "diun":   {"state": "running|stopped|missing", "managed": true, "events": true, "can_install": false, "can_start": false},
 "skopeo": {"state": "ok|missing", "version": "1.x", "can_install": true},
 "radar":  {"state": "active|inactive|missing", "running": false, "can_install": true},
 "cleanup": {"state": "active|inactive|missing", "can_install": true}}
```

- `radar.running`: a radar run is in progress (`diun-radar.service` is `activating`), started by the
  timer, after an update or by “Run radar now”.
- `cleanup.state`: the daily `diun-cleanup.timer`; the plugin shows it in the backups tab, not in the
  requirements panel (the cleanup is optional).
- `diun.managed`: `/opt/diun/docker-compose.yml` exists.
- `diun.events`: `events.jsonl` is not empty.
- `install diun` is only possible on fresh systems: it stops if `/opt/diun` or a Diun container exists.

### Diun event (`events.jsonl`)

```json
{"ts": "2026-10-11T10:00:16Z", "status": "update", "hostname": "docker-host", "provider": "docker",
 "image": "docker.io/ollama/ollama:latest", "digest": "sha256:…", "created": "…",
 "platform": "linux/amd64", "hub_link": "https://hub.docker.com/r/ollama/ollama"}
```

- Written by the script notifier `diun-notify.sh` inside the Diun container: one line with a single
  `printf`, so that parallel notifiers do not mix.
- Diun checks every 6 h (`0 */6 * * *`) with `firstCheckNotif: true`.

## Plugin internals

- **No build step**, no framework, only `cockpit.js` from `../base1/`.
- **Shared design files:** `azentrius.css` (tokens for light and dark, page frame, buttons, cards,
  tables, labels, dialogs), `azentrius-theme.js` (sets `pf-v6-theme-dark` on `<html>` from Cockpit's
  style, `shell:style`), `icons.js` (`window.AzIcons`), the fonts and `OFL.txt`. They are copies of
  `src/` in Azentrius/cockpit-cronjobs, pinned in `tools/design-files.sha256` and never edited here;
  `diun.css` holds only what this page needs on top. Icons are inline SVG from `icon(name)` in
  `diun.js`, all from the shared set.
- **Frame:** header (`az-page-header`, the only `az-btn--primary` is Refresh), tabs (`az-tabs`,
  WAI-ARIA keyboard), `#dm-status` and `#dm-deps` under the tabs, the panels, the signature (version,
  host from `/etc/hostname`). Both tabs are loaded together with every load; switching loads nothing.
- **Status labels:** update / roll-out pending / new version `az-label--info` + `info-circle`, up to
  date `az-label--success` + `check-circle`, unknown `az-label` + `ban`. History results: successful
  success, failed danger, aborted warning, no change and rolled back neutral.
- **Version:** `const VERSION` in `diun.js`; `index.html` loads `diun.js` and `diun.css` with
  `?v=<version>`. `tools/check-version.sh` keeps them and `CHANGELOG.md` in step.
- **Language:** `../base1/po.js` sets `cockpit.language`. `de*` → German, otherwise English.
  - Texts live in `I18N.en` / `I18N.de` in `diun.js`. Static texts in the HTML carry `data-i18n`.
  - The menu entry is translated through `po.manifest.de.js`.
- **Loading:** `safeLoad()` never loads twice; a request during a load is repeated once afterwards.
  - `cockpit.file().watch()` triggers a reload only on a **changed** tag.
  - Background: on 2026-10-12 a watch/read loop started hundreds of inventory processes.
- **Dialog:** one `<dialog>` for everything (install, update, rollback, delete). Flow: `dlg.open` →
  `confirmAndRun` → `dlg.run` → `onDone`.
  - A plan that arrives after the dialog was closed is dropped (`planSeq`).
  - `jump_unknown` in the plan: the warning comes first in the body. A build that failed before (see
    below) adds its own warning with the cause. The confirm button stays disabled until every tick box
    (`.dm-gate`: `#dm-jump-check`, `#dm-major-check`, `#dm-retry-check`) is set. Each warning and its
    tick box form one `.dm-gate-group`; the blocks of the body (groups, plan, dump box, notes) are
    spaced by the flex gap of `.dm-dialog-body`, not by their own margins.
  - `target_version` as a row (“Brings”), with the size of the jump.
  - A `reason` in the result fills `#dm-dialog-reason` (`az-alert--danger`) above the log; every new
    run hides it again.
- **Configured column:** `tag_now.version` follows the tag (“latest = 6.0.2”, `.dm-tagnow`), by the
  same rule as `target_version`: not when the installed version is known but not among the tags.
- **Together:** rows of the same compose project with the same image and tag name each other under
  the container name (“together with …”, three names at most). The name itself is `.dm-name`.
- **Plan groups:** recreated (`containers` without `jobs`), one-off jobs, waits for (`depends_on`),
  watched afterwards (`dependents`); `job_order` as a warning, `shared_networks` as a hint. Neither
  needs a tick box.
- **Failed builds:** from the history, the newest update of each image counts; if it failed, its
  `new_digests` mark the build. A row with Diun's `digest` among them gets a note under the status. The
  history loads after the table, so the rows are drawn again when the marks change.
- **History:** *Show output* (`angle-right`, “opens an entry”) on entries with `log` or `rollback_log`
  runs `diun-update.py log` (`superuser: "try"`) and shows the text in the dialog's log box.
- **Sorting:** update available → roll-out pending → new version → unknown → up to date.
- **Run radar now:** spawns `diun-update.py radar` (`superuser: "require"`, no dialog) and keeps the
  button busy until it returns; `radar.running` from the check shows runs started elsewhere. The new
  `versions.json` reloads the table through the watch. Errors go to the status line.
- **Cleanup box (backups tab):** rules and preview from `backups.cleanup`, the daily timer from
  `diun-deps.sh check` (`install cleanup` to set it up), “Clean up now” runs `cleanup` through the
  dialog.

## Tests

| File | Checks |
|---|---|
| `test/radar_test.py` | `diunlib` (version sources, reach), radar (scheme, comparison, choices for the selection field, hub/registry match with fakes, deeper first lookup, `tag_now`, cache, outage) |
| `test/inv_test.py` | inventory with fake Docker |
| `test/update_test.py` | `diun-update.py` with fake Docker, compose and systemctl: database detection, plan reasons, unknown jump size, argument checks and exit codes, update (dumps, rollback tag, versions, cleanup, radar start), most likely cause of a failure (log lines of several languages, health check, out of memory, `docker compose`), kept output and `log`, digests of the new build, relations in the plan, watch of dependent services, rollback, cleanup rules, `backups`, `radar`, waiting for health, `stream` |
| `test/deps_check_test.py` | `diun-deps.sh check` prints valid JSON with every field the plugin reads |
| `test/loading.js` | loading indicator, double loads, error cases |
| `test/i18n-deps.js` | DE/EN, requirements panel, install dialog |
| `test/update-backups.js` | update buttons, plan dialog, dump tick box, unknown jump size (warning, tick box), rollback offer, cause above the log, backups tab with causes, output button, a build that failed before (row note, second tick box), “together with” in the rows, plan groups and dependencies, delete |
| `test/radar-cleanup.js` | “Run radar now”, cleanup box and “Clean up now”, marks, versions in the history and the update result |
| `test/version-cols.js` | columns Configured / Installed / Newest, chips, legend |
| `test/page-frame.js` | header, tabs (mouse, keyboard, nothing loads), status labels with icons, buttons per place, dialog buttons, signature |
| `test/contrast_test.py` | WCAG contrast of every colour pair the page uses, light and dark (text 4.5:1, borders and icons 3:1) |
| `tools/check-design-files.sh` | the shared design files match `tools/design-files.sha256` |
| `test/harness.js` | prints the whole table (smoke test, also for looking at it) |
| `tools/check-version.sh` | version in `diun.js` = `?v=` in `index.html` = section in `CHANGELOG.md` |
| `tools/check-units.sh` | `systemd-analyze verify` for the unit templates (CI lint step, next to shellcheck and ruff) |
| `test/e2e-server.sh` | real Docker host, throw-away projects `imgupd-test` (Postgres) and `imgupd-test-my` (MariaDB, MySQL) |

The fixtures in `test/fixtures/` come from a real host. Host names are removed; `make_v2.py` builds
`inventory3.jsonl` and `versions2.json` from `inventory2.jsonl`.
