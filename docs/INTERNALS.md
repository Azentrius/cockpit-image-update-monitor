# Internals: interfaces, formats, flows

Reference for everyone working on the code. The helpers are the **only** bridge between the browser and
root. Whoever changes a format changes helper, plugin, fixtures (`test/fixtures/`) and this document
together.

## Paths

| Path | Content | Owner / mode |
|---|---|---|
| `/usr/share/cockpit/image-updates/` | plugin (package `cockpit-image-updates`) | root, 644 |
| `/usr/lib/cockpit-image-updates/` | helpers (`*.py`, `*.sh` 755; `diunlib.py`, `diunproc.py`, `diunstatus.py` 644) | root |
| `/var/lib/diun-radar/versions.json` | result of the version radar | root, 644 (StateDirectory) |
| `/var/lib/diun-radar/status.json` | the add-on's status for the Status Board (since 0.20.0, see [`status`](#status-diun-updatepy-since-0200)) and for the icon in Cockpit's menu (since 0.21.0, see [Status in Cockpit's menu](#status-in-cockpits-menu-since-0210)): names of containers, states and times only | root, 644 |
| `/var/lib/diun/events.jsonl` | Diun events (written by the notifier **inside the container**) | writable from the Diun container → data only, never code |
| `/var/lib/diun/dismissed.json` | Diun events the plugin marked as dismissed | data |
| `/var/backups/image-updates/` | `dumps/<project>/*.sql.gz`, `operations.jsonl`, `logs/<op-id>.log` and `.rollback.log`, `compose/<project>/<stamp>/<n>_<file>` (the compose / `.env` files a tag change wrote, byte-exact as they were) | root, 700 / 600 |
| `/etc/image-updates.conf` | cleanup rules and time limits (optional, see below) | root, not writable by others |
| `/opt/diun/` | Diun stack (`docker-compose.yml`, `diun.yml`), from `diun-deps.sh install diun` | root |
| `/usr/lib/systemd/system/diun-radar.{service,timer}` | radar every 6 h (`*-*-* 00/6:17:00`); from the package, `diun-deps.sh install radar` enables the timer | root, 644 |
| `/usr/lib/systemd/system/diun-cleanup.{service,timer}` | cleanup daily (`04:40` + up to 20 min); from the package, `diun-deps.sh install cleanup` enables the timer | root, 644 |
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
| | `retag <container> <tag> --dry-run` | read (since 0.11.0; the preview in “Change version”) | one JSON object: the `plan` plus `retag`, see below |
| | `retag <container> <tag> --expect <fingerprint> [--no-dump]` | action (since 0.11.0) | text + `@@RESULT {…}` |
| | `status --lang en\|de` | read (since 0.20.0; the Status Board, also **without root**) | one JSON object, schema 1, see [`status`](#status-diun-updatepy-since-0200) |
| `diun-versions.py` | `--output FILE` (systemd), `--table`, without arguments JSON lines | timer / diagnosis | – (`--output` also writes `status.json` next to the file) |

Inputs are checked by regex:

| Input | Regex |
|---|---|
| container name | `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$` |
| operation ID | `^\d{8}-\d{6}-<name>$` |
| dump path | `^<name>/\d{8}-\d{6}_<name>\.sql\.gz$` + `realpath` check |
| rollback tag | `^rollback-\d{8}-\d{6}$` |
| target tag (`retag`) | `^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`, and it must be one of the radar's `choices` |
| fingerprint (`retag --expect`) | `^[0-9a-f]{64}$` |
| language (`status --lang`) | `^[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8})?$`; `de…` is German, every other one English |

`DIUN_LANG` only selects the language of the text output.

`status` is the only verb that runs without root (and without a lock); every other one ends with
exit 77 without it.

### Exit codes (all helpers)

| Code | Meaning |
|---|---|
| 0 | ok (also `nochange`; `cleanup` also when single items could not be removed, see `failed`) |
| 1 | unexpected error of a subcommand (stderr in the output; `radar`: the run failed) |
| 2 | not found / not possible (container, operation, rollback image gone, not updatable, radar not set up, cleanup rules not understood or not root-only) |
| 3 | refused (rolled back already, successfully; file/container exists on `install`) |
| 4 | after update/rollback the containers do not run stably, or `compose up` did not end within `DOCKER_TIMEOUT` (state unclear, `reason.source: "timeout"`), or a signal stopped the restart (`reason.source: "signal"`, see [Signals](#signals)) |
| 5 | stopped before any change (dump failed or did not end within `DUMP_TIMEOUT`, too little space, database not running, the pull failed and left the old image or did not end within `DOCKER_TIMEOUT`, a signal before any container changed; on `rollback`: a file of a tag change edited since, or the compose files giving the services another image) |
| 64 | wrong call |
| 75 | lock taken (already running) |
| 77 | no root rights |
| 128 + n | `diun-update.py` stopped by signal n where the verb has no code of its own for it: 143 SIGTERM, 129 SIGHUP, 130 SIGINT |

### Signals

Since 0.19.0 `diun-update.py` (`entry`) turns SIGTERM, SIGHUP and SIGINT into an exception
(`diunproc.on_signals`, installed only there, not on import), so a run ends through its cleanup as it
does after an error:

- The docker command running (`compose pull`, `compose up`, `rmi`, the `docker exec` of a dump) is
  stopped with its whole process group: SIGTERM, SIGKILL after 10 s, always waited for.
- Before any container changed, the run takes back what it did: dumps (also a partial one), the
  rollback tag, the files of a tag change; exit 5. A rollback stopped before it changed a container
  puts back the files it had restored; exit 5.
- During or after the restart (`compose up`, the wait for health, the watch of dependent services)
  the update fails with its history, output and rollback: `status: "failed"`, `reason.source:
  "signal"`, exit 4. A rollback in that phase is recorded as failed and can be tried again. The cause
  never says that the containers are as before.
- After a successful update a signal skips only the cleanup and the radar start.
- The update lock stays held until all of that is done; then the process ends. Signals that come
  during the cleanup are counted and ignored (the output says how many): they cannot cut it short.
  A history line and the taking back of a run are never cut in two (`diunproc.held`).
- What the stopped command had asked the Docker daemon to do may go on: a pull may finish, `compose
  up` may still recreate containers, `pg_dumpall` or `mariadb-dump` in the container runs until it
  notices that its output is gone.

**SIGKILL** cannot be caught: the helper ends at once, without any cleanup. The run stays in the
history as `running` with its `pid` and its output up to there (`logs/`); once that process is gone
the page shows it as aborted and offers its rollback. The docker command it ran goes on in its own
session. It inherited the update lock (`keep_fds`), so no other update, rollback or cleanup starts
until it has ended as well (exit 75 meanwhile). Check with `docker ps` before acting on such a run.

## Formats

### Inventory – one line per running container

```json
{"name": "glitchtip-postgres", "image": "postgres", "tag": "16-alpine",
 "digest": "sha256:16bc…", "imgid": "sha256:16bc…",
 "version": "16.14", "version_src": "env:PG_VERSION",
 "tagid": "sha256:7218…", "pulled": true, "pulled_version": "16.15",
 "project": "glitchtip", "service": "glitchtip-postgres", "managed": true,
 "files": ["/opt/glitchtip/docker-compose.yml"], "ci_job": false, "created": "2026-08-14T02:11:09.512433187Z",
 "revision": ""}
```

- `image` is the repo without the tag, as written in the compose file.
- `version_src` is `tag`, `env:<NAME>` or `null`. Only the radar knows the label and digest sources.
- A version variable may carry the Debian epoch: `MARIADB_VERSION=1:11.8.9+maria~ubu2404` is 11.8.9
  (`diunlib.core`). Up to 0.17.0 such a variable gave no version. Eclipse Temurin's starts with `jdk-`
  and ends with the build: `JAVA_VERSION=jdk-21.0.12.1+1` is 21.0.12.1, as its tag
  `21.0.12.1_1-jre-jammy` reads. Up to 0.18.0 it gave no version, in the inventory as in `base`.
- `pulled` means: the local tag points to a different image than the running one.
- `managed` means: the compose labels for project, service, working directory and files are present.
- `files`: the compose files the container was started from (the label, `[]` without compose). One
  project can be started from several sets of files one by one; an update takes only the containers
  that share them (see `plan`).
- `ci_job`: the container of a CI job (`diunlib.is_ci_job`): no compose project, and the name
  `FORGEJO-ACTIONS-TASK-<n>_…` / `GITEA-ACTIONS-TASK-<n>_…` (Forgejo runner, Gitea's act_runner; a job's
  service containers carry the prefix too) or a volume at `/var/run/act`. Such containers live as long
  as the job and cannot be updated; the plugin does not list them (see “CI jobs” below).
- `created`: when the image the container runs on was built (Docker's `Created`, `""` if unknown).
- `revision`: the source revision the image names (label `org.opencontainers.image.revision`, else
  `org.label-schema.vcs-ref`; `""` if none). Development builds such as `pomerium:main` carry no
  version, but their commit says which build runs.
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
| `installed` | per **image ID**, also for the newer image that is already pulled. `src` is `tag`, `env:<NAME>`, `label` or `digest`. Digest matches are the cache for the next run: hits are kept for good, `version: null` is retried after 7 days. A **line tag** (since 0.19.0: a tag with two or more numbers under which the registry has longer versions of the same scheme, `v1.6` → `v1.6.74`, `10.0` → `10.0.0.1`, `24.12-alpine` → `24.12.6.70-alpine`) names only its line: its number counts only when neither the label nor the digest names the release, as `{"version": "1.6", "src": "tag", "digest_ts": "…"}` (`digest_ts` keeps the miss for the cache). An exact tag such as `16.14-alpine` has no longer versions under it. |
| `versions[<installed version>]` | newer tags in the scheme of the installed version. `status: "unknown"` when there is no matching scheme (e.g. pgvector `pg17` with `PG_VERSION` 17.10). |
| `versions[…].choices` | (since 0.11.0, for the stage-2 selection field) what can replace the configured tag, in the installed version's scheme and never older: `pin` = the installed version's own tag, `null` if the registry lacks it; `lines` = major and minor lines (`5`, `5.4`, `16-alpine`; one or two blocks, never more) and the newest version tag in each, broad lines first, at most 12: only lines with a released version in them (a bare `9.2` before `9.2.0` may be a pre-release), the running version's own lines always, the others newest first; `newer` = exact tags with `level` `patch` / `minor` / `major`: every newer patch of the installed minor line (at most 10), the newest of each newer minor line and of each newer major (at most 15 each), newest first per level; `floating` = `latest`, `stable`, `production` (with the flavour suffix, e.g. `stable-apache`, or the bare flavour `apache`) when the registry has them. Pre-releases (`-rc.1`), other flavours and `v`-prefixed twins never count. Missing without a scheme and in data from radars before 0.11.0. |
| `by_tag` | comparison with the configured tag itself. Only when the tag contains numbers; the plugin uses it as a fallback. |
| `reach` | levels the configured tag delivers with the Update button. |
| `newest` | highest plain version tag. Shown when the installed version is unknown. |
| `tag_now` | (since 0.10.0) only for floating tags (fewer than two version blocks: `latest`, `16-alpine`, `5`) and, since 0.19.0, line tags (`v1.6`): the digest the tag has in the registry right now (Docker Hub API or `skopeo inspect --raw`, no pull) and the version tag with the same digest (`version` / `tag`, `null` if none). Reused while the digest stays the same; missing when the lookup failed. |
| `tag_now.pushed` / `tag_now.after` | only for a tag without numbers (`latest`, `main`) whose digest no version tag shares, and only on Docker Hub, the one registry that says when a tag was pushed: `after` = the newest release (`newest`) as `{"tag": "v4.5.3", "pushed": "…"}` and `pushed` = when the tag itself was pushed, at least a day later (two builds of one release are minutes apart). MobSF builds `latest` from its development branch. Asked again on every run, also while the digest stays the same. Missing in data from older radars. |
| `status` | `major`, `minor`, `patch`, `current`, `unknown`, `error` or `unlisted`, for sorting and for the table. |
| `stale` / `error` | registry error in this run. The rest of the entry comes from the last good run. In `installed` the new entry wins per image ID, except where it only read the tag's number: without the tag list a line tag cannot be told from an exact one, so the last good entry for the same image ID stays when it names that release or the same number (since 0.19.0; `kept_installed`). Nothing moves to another image ID. |
| `status: "unlisted"` | (since 0.12.2) no registry shows the repository; skopeo answers `requested access to the resource is denied` (Docker Hub, for names it does not have and for private ones), `unauthorized` or `name unknown`. The image was built or tagged on the server (`hibiscus-server:2.12.4` means `docker.io/library/hibiscus-server`), or it is private and the radar has no login. Only `installed` and `error` are set. With a good earlier run the entry is kept like a registry error (`stale`). Timeouts, rate limits and server errors stay `error`. |
| `no_registry` | (top level, since 0.22.0) only when the run reached no registry, not even on its second try: `{"error": <the most frequent error>, "images": <count>}`. Every image ended in a registry error (`unlisted` is an answer and counts as reached); the radar waited `RETRY_PAUSE` (30 s) and tried all of them once more. The entries are then the last good ones, marked `stale`. The page adds “(no registry reached)” to the header with the error in its tooltip; the Status Board's `radar` item warns. |
| `built` / `base` | only for an `unlisted` image built on this server (a compose service with `build:`): `built` is the build time of the running image, `base` the images its Dockerfile starts `FROM` (read through `docker compose config`; stage names, `scratch` and unfilled variables left out; global `ARG` defaults filled in), each `{"image", "tag", "final", "version", "now", "pushed", "newer"}` or with `"pinned": true` (`FROM x@sha256:…`, never newer) or `"error"`. `final` marks the base of the image that runs; `version` is what the build carries (that base's version variable, final base only); `now` what the tag points to (`tag_now` rules, an exact tag is its own version); `pushed` when the registry got it for this server's platform (Docker Hub per platform, so a new list of platforms changes nothing; other registries: the image's creation time). `newer`: pushed more than an hour after the build, or `now` above `version`. Missing in data from older radars. |

How the plugin computes:

```
installed = inventory.version  ||  versions.installed[imgid].version
            (the radar's wins when the inventory read the tag's number and the radar's
            refines it: line tag v1.6, inventory 1.6, radar 1.6.74; same for the pulled image)
compare   = versions.versions[installed]  (if missing or "unknown": by_tag)
newer     = chips patch/minor/major; filled if in reach, otherwise outlined; db+major = grey
status    = "Unknown" instead of "Up to date" when nothing was compared: installed version
            unknown, "unlisted", or a registry error without an earlier result
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
  `built-locally`, `compose-config-error` (+`detail`) or `service-missing` (+`missing`: services the
  compose files do not define, e.g. renamed since the start).
- `services`: the services of the project with the **same** image reference that were started from
  the **same compose files** (label `config_files`, and the project directory). A project split over
  files started one by one (`docker-compose.yml` with `runner`, `runner-2`, `runner-3`,
  `docker-compose.emu.yml` with `runner-emu`) is updated per file set: compose knows only the services
  of the files it is given. The others then run on an image that is already pulled and show “roll-out
  pending”. Up to 0.14.0 the plan took them along, and `compose pull` stopped at once with
  `no such service`.
- `dbs`: all database containers of the project.
  - `kind`: `postgres` or `mysql` when the command starts the server: it names the server, or it
    is options only for the image's `docker-entrypoint.sh` (`--log-bin=binlog`, `-c …`); `unsupported` for
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
    CI job containers are left out: they come and go.
- `ci_jobs`: only for an image of a CI runner (`forgejo/runner`, `gitea/act_runner`,
  `diunlib.CI_RUNNER_RE`): the CI job containers that run now, `[{"name", "image", "started"}]`. The
  update restarts the runners and those jobs break off; their containers stay behind without a runner
  (seen on a CI host, 2026-10-17). `[]` otherwise.

### `retag --dry-run` (diun-update.py, since 0.11.0)

The `plan` of the container (above) with `target_version`, `jump` and `jump_unknown` for the new tag,
plus:

```json
"retag": {"tag": "5", "from_tag": "5.0.2", "image": "ghcr.io/formbricks/formbricks:5",
          "choice": "line", "level": null, "newest": "5.4.5",
          "possible": true, "reason": null, "detail": null, "variable": null,
          "changes": [{"file": "/opt/formbricks/docker-compose.yml", "line": 105,
                       "old": "    image: ghcr.io/formbricks/formbricks:5.0.2",
                       "new": "    image: ghcr.io/formbricks/formbricks:5",
                       "service": "formbricks", "sha256": "…"}],
          "git": null, "fingerprint": "…"}
```

- `choice`: under which the radar offers the tag for the installed version (`versions[…].choices`):
  `pin`, `newer` (with `level`), `line` (with `newest`, whose version is `target_version`) or
  `floating` (`target_version` `null`, `jump_unknown` true).
- `changes`: every line that would change, 1-based, before and after, with the file's sha256 at
  planning time. Nothing is written. Where the tag is found, in the text of the files (no YAML
  library, no round trip, so comments, quotes, order and line endings stay):
  - `image: repo:tag` as a direct key of a service: that line, for every service of the image
    (one-off jobs such as `migrate` too), in whichever compose file it is.
  - `image: repo:${VAR}` or `image: ${VAR}`: the variable's line in the `.env` file (label
    `…environment_file`), when it is set there exactly once and used nowhere else; `service` is `null`
    and `variable` names it.
  Without an env file label the `.env` of the project directory counts, as compose reads it then.
- `git`: the git repository the project directory lies in, as a hint (nothing is committed).
- `fingerprint`: sha256 over the new tag and every change (file, line, before, after, file
  checksum). The page passes it to `retag … --expect`.
- `reason` when `possible` is false, `detail` names the file:line, service or variable:

| `reason` | Meaning |
|---|---|
| a `plan` reason | not updatable at all (`not-compose`, `digest-pinned`, …) |
| `same-tag` | the configured tag itself |
| `no-choices` | the radar has no choices for the installed version (unknown version, no scheme, data before 0.11.0) |
| `not-offered` | not among the choices: older, a pre-release, another flavour |
| `db-major` | a database image to another major version, or to a floating tag that can bring one (stage 5) |
| `major-skip` | Nextcloud more than one major ahead, or to a floating tag |
| `anchors` | the image line sits under a YAML anchor or outside a service |
| `not-found` | no `image:` line with this reference for a service (e.g. `include:` or a generated file) |
| `variable-complex` | a variable that is not in the `.env` file, set twice, only a default, or a reference built from several variables |
| `variable-shared` | the variable is also used elsewhere |
| `unreadable` | a compose file is no regular UTF-8 file of at most 1 MiB |

### `retag … --expect <fingerprint>` (the tag change, since 0.11.0)

Takes the update lock, makes the plan again and stops with exit 5 before anything changes when the tag
change is no longer possible or the fingerprint differs (a file edited since the preview). Then, as
`update` with these differences:

1. Dumps (unless `--no-dump`), rollback tag of the running image.
2. **The files:** byte-exact copies to `compose/<project>/<stamp>/<n>_<name>` (folders 700, files
   600); each planned line replaced in the text (line endings kept), written atomically (temporary file
   in the same folder, owner and mode of the original, `rename`); a symlinked file is written where
   the link points. Then `docker compose config`: every service of the image must resolve to the new
   reference. If not, or if writing fails, the copies are put back, the rollback tag and dumps removed,
   exit 5: no container changed.
3. Pull; the new image must then exist locally, otherwise the same: files back, exit 5.
4. `up -d --no-deps` always, even when the image ID stays the same (pinning `latest` to the running
   version): the containers must carry the new tag. Waiting, watching the dependents, cleanup and radar
   as in `update`.

A rollback of a tag change first checks every file against the checksum written (`new_sha256`) and
every copy against the original's (`sha256`): a file edited after the change, or a damaged copy,
stops it (exit 5 / 2) without touching anything. A file that has its old checksum already stays as it
is (since 0.18.1: an earlier attempt put it back, or the run ended before writing it). Then the copies
go back, the old tag points to the rollback image again and the services are recreated. The copies
stay with the history entry.

### `rollback <op-id>`

Takes the update lock. Before anything changes (otherwise exit 2 / 3 / 5, nothing touched):

- the operation is an `update` or `retag` that is not rolled back yet, or whose rollback failed: a
  failed rollback may be tried again (since 0.18.1; up to 0.18.0 it was marked `rolled_back` too and
  refused with exit 3);
- the rollback image exists;
- the files of a tag change (above);
- since 0.18.1 `docker compose config` gives every service of the operation its `image`, for a tag
  change once its copies are back (if not, the files are written again as they were before the
  attempt). Otherwise compose would start another image than the rollback copy, which only takes
  that tag back: an `image:` line changed after the update (`acme/app:latest` → `acme/app:2`), a
  `.env` edited, a service removed. Exit 5 names the services and the images they get now.

Then the tag points to the rollback image, `up -d --no-deps --pull never`, waiting for health, and
since 0.18.1 every container of the services (one-off `compose run` containers aside) must run the
rollback image's ID: one that runs another image fails the rollback (exit 4, `reason.source`
`"image"`).

### `@@RESULT` (last line of `update` / `rollback` / `cleanup`)

```
update:   @@RESULT {"op": "20261012-015025-imgupd-test-db", "status": "ok|nochange|failed",
                    "rollback": "repo:rollback-20261012-015025", "dumps": ["project/…sql.gz"],
                    "from_version": "16.14", "to_version": "16.15"}
          (on failed also "reason": {…}, see below)
retag:    as update, status "ok|failed", plus "image": "repo:<new tag>" and
          "backup": "/var/backups/image-updates/compose/<project>/<stamp>"
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
  unhealthy container), `compose` (the last 50 lines `docker compose up` printed), `timeout` (since
  0.19.0: `compose up` did not end within `DOCKER_TIMEOUT`; `container` is `null`), `error` (since
  0.18.1: the helper itself stopped after the containers were recreated, e.g. Docker did not answer;
  `container` is `null`), `signal` (since 0.19.0: SIGTERM, SIGHUP or SIGINT stopped the restart or the
  check after it, see [Signals](#signals); `container` is `null`), `image` (since 0.18.1, rollback only:
  the container runs another image than the rollback copy) or `null`.
- `line` (`reason_line`): the last line that names an error (`Error:`, `TypeError:`,
  `…Exception`, `error:`, `panic:`, `FATAL`, `PANIC`, `CRITICAL`), plus the next line when it ends in
  `:`; otherwise the last line that mentions a problem (`error`, `failed`, `refused`, `not found` …).
  Stack frames never count, colour codes are removed, at most 300 characters. `null` when nothing fits;
  the plugin then names the state (out of memory, exit code, not ready in time).

### `status` (diun-update.py, since 0.20.0)

The shared status contract of the Azentrius add-ons, schema 1: the Status Board on the Cockpit bastion
calls `status --lang <xx>` on every host with `superuser: "try"` and shows a tile per host and add-on.
It finds the command in `plugin/manifest.json`:

```json
"azentrius": {"status": ["/usr/lib/cockpit-image-updates/diun-update.py", "status"]}
```

The answer, here for the real host data of `test/fixtures/` (`test/fixtures/status.json`, the file as
written, with its `facts`):

```json
{"schema": 1, "addon": "image-updates", "label": "Image Update Monitor", "version": "0.20.0",
 "checked_at": "2026-10-12T02:50:00Z", "state": "info",
 "summary": "2 updates available, 3 roll-outs pending, 17 new versions",
 "items": [
   {"id": "updates", "state": "info", "title": "Updates available", "value": "2",
    "detail": "ollama, prod-mcp-1", "checked_at": "2026-10-12T02:50:00Z", "stale_after": 86400},
   {"id": "failed", "state": "ok", "title": "Failed updates", "value": "0",
    "checked_at": "2026-10-12T02:50:00Z", "stale_after": 86400},
   {"id": "radar", "state": "ok", "title": "Version radar", "value": "< 1 h",
    "checked_at": "2026-10-12T02:17:34Z", "stale_after": 86400},
   {"id": "rollout", "state": "info", "title": "Roll-out pending", "value": "3",
    "detail": "docuseal-db, glitchtip-postgres, plausible_db", "checked_at": "2026-10-12T02:50:00Z",
    "stale_after": 86400},
   {"id": "new-versions", "state": "info", "title": "New versions", "value": "17",
    "detail": "actual-ai, actual_server, calcom-db, diun-socket-proxy, docuseal +12",
    "checked_at": "2026-10-12T02:17:34Z", "stale_after": 86400}]}
```

| Item | State | Counts |
|---|---|---|
| `updates` | `info` with containers, else `ok`; `none` while Diun has written no event | the rows the page marks “Update available”: Diun's newest event of the image and tag names another digest than the one running, not dismissed and not the same build (`sameBuild`) |
| `failed` | `danger` with entries, else `ok` | the `update` and `retag` runs of the history that failed and were not rolled back, whose rollback failed, or that ended without their last line (`running` while no process runs it: aborted), as long as no later run settled them. A run stands for what `make_plan` grouped: its `services` in its compose project and compose files (`workdir`, `files`), and the containers it names. A later run (any outcome, also one still in progress) settles only those services in the same files (or where the files are not recorded) and the containers it names: a success of another service, another tag of the image or the same image in other compose files settles nothing; a later run of the same services, also a tag change, does. A run partly settled is named by a container still affected, with the services still affected. Entries without `services` (older versions) stand for the containers they name, and they settle no services |
| `radar` | `ok`; `warning` when `versions.json` is older than 24 hours (the page's “outdated”) or its time is unreadable, or (since 0.22.0) when its last run reached no registry (`no_registry`; title “Version radar reached no registry”, the detail points to DNS and the network, never the error itself, which can name addresses); `none` without `versions.json` | `value` is the age (“2 h”, “3 days”) |
| `rollout` | `info` with containers, else `ok` | the rows “Roll-out pending” (`pulled`) |
| `new-versions` | `info` with containers, else `ok`; `none` without `versions.json` | the rows “New version”: a newer patch or minor, a major of an image that is no database, or a newer base of an image built here |

- **The page's rules.** `updates`, `rollout` and `new-versions` are the rows of the page's status
  column (`rowFor`, `versionCell`, `installedOf` in `diun.js`, ported to `diunstatus.py`); CI job
  containers are no rows. `test/status-rows.js` checks with the fixtures that the page shows exactly
  the rows `status.json` names. A rule that changes on the page changes there as well.
- **State and summary** as the contract says: the gravest of `danger` > `warning` > `info`, otherwise
  `ok` when an item is `ok`, otherwise `none`. The summary names the findings, the gravest first (“1
  failed update, version radar outdated, 2 updates available”), or says “No updates or problems
  found”.
- `detail`: at most five names, then “+N”; `value`: a number as text, the radar's age; no `link` (the
  page has no addresses for its tabs). `stale_after` is 86400 on every item.
- **Who writes `status.json`:** the radar run (`diun-versions.py --output`, next to `versions.json`; a
  problem there only prints `status not written: …` and never fails the run) and `diun-update.py`
  after `update`, `retag … --expect`, `rollback` and `cleanup`, also after a failure, but not after a
  wrong call, a taken lock or without root (exit 64, 75, 77). That write is silent, so `@@RESULT`
  stays the last line; a problem (Docker not answering) keeps the last file. It reads the inventory
  (`records()` of `diun-inventory.py`, without its lock), `events.jsonl` and `dismissed.json` (as data:
  only regular files, no link, no pipe; of `events.jsonl` the newest 16 MiB), `versions.json` and the
  history. Written atomically (temporary file in the same folder, `rename`), 0644.
- **Its content:** the contract's JSON in English plus `facts`, the same without words: per item the
  container names (`null` where the source is missing), the failed runs as `{"name", "how"}` (`how`:
  `failed`, `rollback`, `aborted`; since 0.21.2 with `"services"`, the services still affected, when a
  later run settled the others; the detail then says “(still affected: db)”), the radar's `generated` and the times. Names of containers only:
  no compose paths, projects, addresses or user names.
- **`status` reads only files:** no Docker, no lock, also without root. `status.json`, rendered from its
  `facts` in the language asked for; `versions.json` again for the radar's age now; as root also the
  history again for `failed` (cheap, and it catches a killed run before the next write). `version` is
  the page's (`const VERSION` in `diun.js`). Without `status.json` the state is `none`, without items,
  and the summary says why (the radar is not set up or has not run yet, or its next run brings it); an
  unreadable file is `none` as well. Exit 0 then too, and for `danger`; 64 for a wrong call.
- Every text exists in English and in German (with “Sie”); the tests check that the English ones carry
  no German.
- **Also in Cockpit's menu** (since 0.21.0): the page shows the same state and summary as the status
  icon of its menu entry, from this file, see [Status in Cockpit's menu](#status-in-cockpits-menu-since-0210).

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
- **Time limits** (since 0.19.0, `read_conf` / `load_limits`): `DOCKER_TIMEOUT` (default 1800,
  60–86400 seconds) for one streamed docker command (`compose pull`, `compose up`, `rmi`),
  `DUMP_TIMEOUT` (default 14400, 60–172800) for one database dump. `update`, `retag --expect`,
  `rollback` and `delete-image` read them first; a value out of range or not a number stops them with
  exit 2 before anything changes. `lib/diunproc.py` runs the command in a process group of its own;
  the limit is a deadline for the whole command, also without any output. Then SIGTERM to the group,
  after 10 s SIGKILL, and the process is always waited for. A pull or dump over its limit stops the
  update before any change (exit 5, a partial dump removed); `compose up` over its limit fails it
  (exit 4, history, output and rollback kept), and the cause says the containers may still be
  changing, because the Docker daemon goes on with what the stopped client asked for.
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
rollback marks the operation with `rolled_back`, `rollback_ts`, `rollback_status` and
`rollback_reason`. Each line is flushed to disk (`fsync`) before the run goes on.

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
  (label / digest), otherwise `null`. Older entries do not have them; the plugin shows “—”. Since the
  radar names a new image only after the update, `backups` fills a `null` version from the radar's
  `installed` entry for `old_id` / `new_id`, and since 0.19.0 also replaces a line tag's number that
  the radar refines (`1.6` → `1.6.74`; the history file stays as written). `update`, `retag` and
  `plan` take the radar's version over the tag's number the same way (`version_of`).
- `reason` (since 0.8.0): on a failed update, the same object as in `@@RESULT`. A rollback adds
  `rollback_status` (`ok` / `failed`) and `rollback_reason` (`null` when it went through). Since 0.18.1
  `rolled_back` is `true` only when it went through; a failed one may be tried again, and its output is
  added to the first one's (`--- Tried again ---`). Up to 0.18.0 a failed rollback was marked
  `rolled_back: true` too; `rollback` and `backups` treat such lines as failed. The backups tab shows
  the cause under the result (the rollback's if it failed, otherwise the update's).
- **`status`**: `running` from the start, then `ok`, `failed` or `nochange`. Since 0.18.1 the run is
  in the history **before the first change a rollback has to undo**: a tag change before it writes
  its files (with `backup` and `backup_files`), every update once more before `up` (with `new_id`).
  Whatever ends the run from then on, the entry stays with its rollback image and copies:
  - an exception after the containers were recreated (Docker does not answer) fails the update (exit
    4) with `reason.source: "error"`;
  - a run that ends without its last line (killed, power cut) stays `running`. `pid` names its process;
    `backups` adds `active` (that process still runs this helper) and offers the rollback only when it
    does not. The page shows such an entry as “aborted”, or “running” while it is active.
  A run that stops before any container changed takes back what it did (files from their copies,
  copies, rollback tag, dumps); if it was in the history already, a line with `status: "discarded"`
  hides it (`load_ops` leaves it out) and no output is kept.
- **Dependent services** (since 0.9.0): after the recreated containers are healthy, `update` checks
  the running containers of `dependents` five times, 3 seconds apart, against their state from before
  the update (`StartedAt`, `RestartCount`, not `unhealthy`). One that stopped, restarted or turned
  unhealthy makes the update fail (exit 4) with its own `reason`.
- `new_digests` (since 0.9.0): the registry digests of the new image (`RepoDigests`, `sha256:…`), the
  values Diun reports as `digest`. `[]` for images built or tagged locally.
- **Tag change** (since 0.11.0): `kind: "retag"`, `image` is the old reference, plus `to_image`,
  `from_tag`, `to_tag`, `changes` (`file`, `line`, `old`, `new`), `backup` (the folder) and
  `backup_files` (`path`, `backup`, `sha256` before, `new_sha256` after). `backups` leaves
  `backup_files` out. Its dumps count as the project's last update for the cleanup.
- **Output:** `update` and `rollback`, called through `main`, keep what they print in
  `logs/<op-id>.log` and `logs/<op-id>.rollback.log` (600, at most 512 KiB, without `@@RESULT`). Only
  runs that reach the history keep output; an update stopped before any change does not, and a run that
  took back what it did (`discarded`) removes its file again. The files stay with their history entry;
  nothing deletes them.
  Since 0.19.0 the output goes to the file as it is printed, from the moment the run is in the history
  (an update: before a tag change writes its files or before the containers are recreated; a rollback:
  before it points the tag back), what was printed before first; the live output on stdout is the
  same as before. A killed run (SIGKILL) keeps it up to its last complete line; up to 0.18.1 the file
  was written only at the end and stayed empty then. The file reaches the disk (`fsync`) with every
  history line and at the end; after a power cut the lines printed since the last history line can be
  missing, the history line itself not. A rollback tried again adds its output after a
  `--- Tried again ---` line.

`backups` returns the last 30 operations, with `can_rollback`, `active`, `log` and `rollback_log`
(whether output is kept) added and without paths, image IDs and `pid`.
On top come `dumps[]` (`path`, `project`, `container`, `size`, `mtime`), `images[]` (`ref`, `id`,
`size`, `created`, `in_use`), `dump_bytes`, `free_bytes`, `dir` and `cleanup`. An image's `size` is in
bytes (`docker image inspect`, `null` if Docker does not say) and `created` is ISO 8601
(`2026-10-12T03:15:00+02:00`, empty if unreadable); up to 0.12.2 they were Docker's text (`275MB`,
`2026-10-12 03:15:00 +0200 CEST`), which the page still shows as it is:

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
- Which row an event belongs to: the plugin keeps the newest event per image **and tag** (`verKey`,
  `postgres:16`); a row takes only the event of its own tag. An event whose `image` carries a digest
  (`postgres@sha256:…`, `postgres:18.3-alpine@sha256:…`) belongs to no row: a pinned image does not
  follow its tag, so Update cannot bring what Diun saw (the plan says `digest-pinned`). Service
  containers of CI jobs produce such events. The registry link (`hub_link`) is the image's, from any of
  its events. Up to 0.13.0 the newest event of the image counted for every tag.
- A new digest is no update when the event's `created` is the running image's `created` (to the
  second, any time zone; `builtAt`, `sameBuild`): Docker Hub publishes the list of platforms of an
  official image again when it adds one (`nextcloud:apache` got riscv64, 2026-10-16), and the list's
  digest changes while the amd64 build stays. A date before 1980 counts for nothing: reproducible
  builds set a fixed one (1970-01-01).

## Plugin internals

- **No build step**, no framework, only `cockpit.js` from `../base1/`.
- **Shared design files:** `azentrius.css` (tokens for light and dark, page frame, buttons, cards,
  tables, labels, dialogs), `azentrius-theme.js` (sets `pf-v6-theme-dark` on `<html>` from Cockpit's
  style, `shell:style`), `icons.js` (`window.AzIcons`), the fonts and `OFL.txt`. They are copies of
  `src/` in Azentrius/cockpit-cronjobs, pinned in `tools/design-files.sha256` and never edited here;
  `diun.css` holds only what this page needs on top. Icons are inline SVG from `icon(name)` in
  `diun.js`, all from the shared set.
- **Fonts:** DM Sans, DM Serif Display and Red Hat Mono (code and versions) ship with the add-on.
  The page requests no font from Cockpit: its static files differ between Cockpit builds (Ubuntu
  24.04's Cockpit 314 has no Red Hat Mono and answered 404 when `azentrius.css` took it from there).
  `test/fonts_test.py` checks that every font the page uses ships with it.
- **Frame:** header (`az-page-header`, the only `az-btn--primary` is Refresh), tabs (`az-tabs`,
  WAI-ARIA keyboard), `#dm-status` and `#dm-deps` under the tabs, the panels, the signature (version,
  host from `/etc/hostname`, both written when the page starts). Both tabs are loaded together with
  every load; switching loads nothing.
- **Status labels:** update / roll-out pending / new version `az-label--info` + `info-circle`, up to
  date `az-label--success` + `check-circle`, unknown `az-label` + `ban`. History results: successful
  success, failed danger, aborted warning, no change and rolled back neutral.
- **Version:** `const VERSION` in `diun.js`; `index.html` loads `diun.js` and `diun.css` with
  `?v=<version>`. `tools/check-version.sh` keeps them and `CHANGELOG.md` in step.
- **Manifest:** besides the menu entry, `azentrius.status` names the status command for the Status
  Board (see [`status`](#status-diun-updatepy-since-0200)); Cockpit ignores the section. `"preload":
  ["index"]` (since 0.21.0) loads the page hidden after the login, for the status icon in the menu
  (see [Status in Cockpit's menu](#status-in-cockpits-menu-since-0210)).
- **Language:** `../base1/po.js` sets `cockpit.language`. `de*` → German, otherwise English.
  - Texts live in `I18N.en` / `I18N.de` in `i18n.js` (since 0.19.0; `window.ImageUpdatesI18n`, loaded
    before `menu-status.js` and `diun.js`, which keep only the code). Static texts in the HTML carry
    `data-i18n`.
  - The menu entry is the product name, Image Update Monitor, and is not translated (`po.manifest.de.js` stays empty).
- **Loading:** `safeLoad()` never loads twice; a request during a load is repeated once afterwards.
  - `cockpit.file().watch()` triggers a reload only on a **changed** tag.
  - Background: on 2026-10-12 a watch/read loop started hundreds of inventory processes.
- **Dialog:** one `<dialog>` for everything (install, update, version change, rollback, delete). Flow: `dlg.open` →
  `confirmAndRun` → `dlg.run` → `onDone`.
  - A plan that arrives after the dialog was closed is dropped (`planSeq`).
  - `jump_unknown` in the plan: the warning comes first in the body. A build that failed before (see
    below) adds its own warning with the cause. The confirm button stays disabled until every tick box
    (`.dm-gate`: `#dm-jump-check`, `#dm-major-check`, `#dm-retry-check`, `#dm-ci-check`) is set. Each warning and its
    tick box form one `.dm-gate-group`; the blocks of the body (groups, plan, dump box, notes) are
    spaced by the flex gap of `.dm-dialog-body`, not by their own margins.
  - `target_version` as a row (“Brings”), with the size of the jump.
  - A `reason` in the result fills `#dm-dialog-reason` (`az-alert--danger`) above the log; every new
    run hides it again.
- **Built here (`base`):** for an `unlisted` record with `base`, the newest column says “base newer: caddy:2 = 2.11.7”
  (`az-label--info`, the row counts as a new version) when a base is `newer`, the final one named first;
  otherwise “base current” with `check-circle`, and the row is up to date although its version is
  unknown (`baseChecked`). The tooltip names the build date, each base with its role, what it points to,
  when it was published and what the build carries, and how to rebuild
  (`docker compose build --pull <service>`, then `up -d`, in the directory of the compose file). With
  no base checked (registry errors) it says “base not checked”.
- **Build after a release:** with `tag_now.after` and the installed version unknown, the newest column
  says “build after v4.5.3” instead of “newest: v4.5.3”, only while `after.tag` is still `newest`. If
  the running image's digest is `tag_now.digest`, it carries `check-circle` and the status is “up to
  date” (`sameBuild`): the container runs what the tag points to, and that is newer than every
  release. Otherwise the text is muted and the status stays unknown.
- **Newest build (no release tag at all):** with `tag_now` without `version`, no `after` and no
  `newest` (the registry has no plain version tag, e.g. an own image on ghcr.io built from its main
  branch), the newest column says “newest build from <date>” (the image's `created`; without the date
  for a fixed date before 1980) with `check-circle` and the status is “up to date” (`sameBuild`) when
  the running image's digest is `tag_now.digest`; otherwise “other build”, muted,
  and the status stays unknown. With release tags in the registry outside Docker Hub nothing tells
  whether the build is newer than the newest release, so the column keeps “newest: X”.
- **Stale data in the build cells:** when the radar could not reach the registry, `keep_last_good`
  keeps `tag_now` from the run before with `stale`, `error` and that run's `checked`. “Newest build”,
  “other build” and “build after vX” then carry “(older data)” like every stale entry; its tooltip
  names the error and when the data was found (“The radar's last run could not reach the registry: …
  Shown is what it found on …”). The cell's tooltip says what the last good run found (“At the last
  successful check the container ran this build; whether there is anything newer now is not known”)
  instead of “there is nothing newer”. The status follows the kept data, as for every stale entry;
  the next good run clears the mark.
- **Configured column:** `tag_now.version` follows the tag (“latest = 6.0.2”, `.dm-tagnow`), by the
  same rule as `target_version`: not when the installed version is known but not among the tags.
- **Development builds:** without an installed version, a row whose image names a `revision` shows
  “<tag> @<7 characters>” in the installed column (“main @35b2a97”), with the full revision and the
  build date in the tooltip. A known version always wins.
- **CI jobs:** rows with `ci_job` are not listed. The meta line counts them (“2 CI jobs hidden”) and
  its tooltip names them (“unit (2415)”, from `…_JOB-<job>` and the task number). A plan with `ci_jobs`
  gets the warning “CI jobs are running” with the jobs and how long they have run, and the tick box
  `#dm-ci-check`.
- **Together:** rows of the same compose project with the same image and tag and the same compose
  files (`files`) name each other under the container name (“together with …”, three names at most). The name itself is `.dm-name`.
- **Plan groups:** recreated (`containers` without `jobs`), one-off jobs, waits for (`depends_on`),
  watched afterwards (`dependents`); `job_order` as a warning, `shared_networks` as a hint. Neither
  needs a tick box.
- **Change version** (since 0.11.0): rows of compose containers whose installed version has
  `choices` get the icon button `exchange-alt`; an outlined chip whose tag is among them is a button
  (`data-retag`, `data-tag`) that opens the same dialog with that tag chosen.
  - The field (`#dm-retag-select`): the configured tag first (“configured now”, nothing to do), then
    the groups pin, lines (“currently <newest>”), patch, minor, major and the floating tags. The
    configured tag itself is left out of the groups.
  - Listed but locked (`disabled`, the reason in the entry): on a database (`db`) every target in
    another major and every floating tag; on Nextcloud (`(^|/)nextcloud$`) more than one major ahead
    and every floating tag. The helper refuses the same (`db-major`, `major-skip`). The major is the
    first number of the tag (`v3.2.1` → 3, `18-alpine` → 18).
  - Each choice runs `retag … --dry-run` (`superuser: "try"`); an answer for an earlier choice is
    dropped. `#dm-retag-preview` shows the changed lines (file:line, `-` before, `+` after, wrapped,
    never scrolled sideways), `variable` and `git` as hints, then the plan of an update. A floating tag
    (`jump_unknown`) has its own warning and tick box; a major the one of an update. A refusal names
    `reason.<reason>`, an unknown reason as it is.
  - Confirming runs `retag <c> <tag> --expect <fingerprint>` (`--no-dump` without the dump box) with
    the fingerprint of the preview shown. From the start of a run the field and the tick boxes are
    locked (every run of the dialog). Exit 4 offers the rollback as an update does; exit 5 means
    stopped before any change.
  - History: kind `retag` gets “Tag changed: <from_tag> → <to_tag>” under the image; its rollback
    dialog names the files it puts back (`changes[].file`). A rollback that stops with exit 2 or 5
    says that nothing changed. Failed builds count only kind `update`.
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

### Status in Cockpit's menu (since 0.21.0)

The menu entry “Image Update Monitor” carries the add-on's state as Cockpit's own status icon, for the
host whose menu is shown: `danger` as `error` (red), `warning` as `warning` (yellow), `info` as `info`
(blue); `ok` and `none` show none. The tooltip is the summary of
[`status`](#status-diun-updatepy-since-0200) in the session language (“2 updates available, 3
roll-outs pending, 17 new versions”).

- **How Cockpit does it** (read in its code, tags 264, 316 and 360): a page sends
  `cockpit.transport.control("notify", {page_status: {type, title}})`. The shell files it under the
  host and page of the frame that sent it (from the frame's name `cockpit1:<host>/<page>`,
  `register()` in `pkg/shell/base_index.js` up to 316, `pkg/shell/router.jsx` in 360), keeps it in
  `sessionStorage["cockpit:page_status"]` and draws the icon at the menu entry whose path is that page:
  `image-updates` for `menu.index`. `"preload": ["index"]` in `manifest.json` makes the shell load the
  page right after the login, hidden, as Cockpit's own Software updates page does.
- **Why the page itself:** a small preloaded component of its own (`"preload": ["status"]`,
  `status.html`) would run as the page `image-updates/status`, and no menu entry reads that status.
  Only the page behind the entry can set it. So `index.html` loads, but until the page is shown only
  `menu-status.js` works.
- **`menu-status.js`** watches `/var/lib/diun-radar/status.json` (`cockpit.file`, `superuser: "try"`;
  the file is 644, so it works without administrative access too) and reads nothing else. It judges
  the state as `status` does, from the file's `facts`: the items' counts, and the radar outdated after
  24 hours as of now, checked again the moment it turns 24 hours old. Two differences: the radar's
  time comes from `facts.radar` (the radar run writes it together with `versions.json`), not from
  `versions.json`, and `failed` is not read again from the history (only root could). A missing or
  unreadable file shows no icon. A status is sent only when it changed, the first one always. The
  texts are the `menu.*` keys in `i18n.js`, worded as the summary in `diunstatus.py`;
  `test/menu-status.js` compares both for the same facts.
- **When the page starts:** `diun.js` (inventory, the watches of the Diun log and the radar file, the
  helpers, the signature) waits for `ImageUpdatesMenuStatus.shown`, the first time `cockpit.hidden` is
  false. That flag holds only once the shell's hint has arrived: Cockpit 264 to 316 sends a frame that
  is not the current one `{hidden: true}` right after the reply to its `init` and the current one
  nothing; 360 sends every frame `{hidden}` then. Messages from the shell arrive in order, so when the
  first answer about `status.json` is back, a hint that was sent has arrived. Without any answer the
  page decides after 3 seconds. Opened without the shell, it starts once the answer is back.
- **Several hosts** (Cockpit's host switcher): the shell preloads the page on every connected host that
  has the add-on, and each sets the icon in that host's menu. That runs the page's code from that host
  once it is connected, as Cockpit's own preloaded Software updates page does.

## Tests

| File | Checks |
|---|---|
| `test/radar_test.py` | `diunlib` (version sources, reach), radar (scheme, comparison, choices for the selection field, hub/registry match with fakes, deeper first lookup, `tag_now`, line tags (release by digest, the tag's number on a miss, exact tags untouched, a registry outage keeping the release per image ID), a build after the newest release, the bases of an image built here (Dockerfile `FROM` lines, push times, newer or not), cache, outage) |
| `test/inv_test.py` | inventory with fake Docker, compose files per container, CI job containers, build time, source revision |
| `test/status_test.py` | `diunstatus.py` and `diun-update.py status`: the page's rules for the rows (Diun's event of the tag, `docker.io/library/`, another tag, a digest reference, dismissed, the same build in Docker's and Diun's time, a date before 1980, the newest event, roll-out first, CI jobs, no digest, a newer patch, a database major, `by_tag`, an empty comparison, a line tag refined by the radar, the base of an image built here), failed updates from the history (rolled back, a failed rollback also as written up to 0.18.0, aborted, still running, `discarded`; which later run settles which: another service and tag of the same project, the same image in other compose files or another directory, a retry and a tag change of the same services, overlapping groups, rollbacks and aborted runs next to an unrelated success, entries without services; the resulting state, summary and detail), the radar's age, state and summary in English and German with plurals, the contract's shape, no German in English, the fixture `status.json` of real host data, `status` without the file, with an unreadable one, from the file in German, the radar's age judged when asked, the history as root and the file without root, Docker not answering (the last file stays), `events.jsonl` as a link or a pipe and a large one, the arguments (exit 64, no root needed), the write after `rollback` and `cleanup` (silent) and none after `--dry-run`, a wrong call or without root, the radar run writing it and not failing for it |
| `test/status-rows.js` | the page with the fixtures of a real host marks exactly the containers “Update available”, “Roll-out pending” and “New version” that `test/fixtures/status.json` names |
| `test/menu-status.js` | the status icon in Cockpit's menu with `i18n.js`, `menu-status.js` and `diun.js`: preloaded and hidden, the icon from `status.json` (`superuser: "try"`) and nothing else read or run, shown, the page loads (once); opened directly; German; a changed file, the same again (not sent), a missing and an unreadable file; the radar turning outdated while the session stays open (the check at 24 hours); no answer at all (the page starts after 3 seconds); state and tooltip equal state and summary of `diunstatus.document()` for the same facts, in English and German (the fixture, failed updates with an outdated radar, singular forms, nothing to do, no Diun and no radar, an unreadable radar time, a time with an offset, odd values) |
| `test/proc_test.py` | `diunproc` with real child processes: a silent hang, endless output, children that ignore SIGTERM (the group killed), a dump that hangs, the next command after a timeout; signals: no handler changed on import, `on_signals` restores the handlers before it, `held` defers a signal; a helper that holds a lock gets SIGTERM or SIGHUP while its command runs (silent and printing, `stream` and `copy`) and keeps the lock until the stop has reached the command (synchronised through FIFOs), children that ignore SIGTERM with SIGTERM and SIGHUP again during the cleanup (SIGKILL after the grace time, repeats only counted), the next operation afterwards, SIGKILL (the command runs on and keeps the lock until it ends) |
| `test/update_test.py` | `diun-update.py` with fake Docker, compose and systemctl: database detection, plan reasons, services of the same compose files only, running CI jobs in a runner's plan, unknown jump size, argument checks and exit codes, update (dumps, rollback tag, versions, cleanup, radar start, a failed pull that leaves the old image, a registry error with and without a newer image on the server), most likely cause of a failure (log lines of several languages, health check, out of memory, `docker compose`), kept output and `log`, digests of the new build, relations in the plan, watch of dependent services, rollback (a failed one tried again, an `image:` line changed since, containers on another image), a run cut off after the restart (exception, kill) or before it (files back, entry discarded), cleanup rules, `backups`, `radar`, `retag --dry-run` (tag lines, override files, `.env` variables, anchors, refusals, nothing written), `retag --expect` (fingerprint, the written lines with owner and mode, backup, a failed `compose config` or pull restoring the files, an override file, rollback restoring them byte-exact, a file edited after the change stopping the rollback), waiting for health, `stream`, a real update process killed with SIGKILL after the restart (history, kept output, rollback; synchronised through pipes), signals (SIGTERM during the dump: exit 5, partial dump and everything else taken back; SIGHUP during the pull: exit 5; SIGTERM during `compose up`: exit 4 with history, output, rollback and the cause `signal`; SIGHUP during a rollback's restart: failed, offered again; a signal before the rollback changed a container: exit 5; the same with the real helper (`entry`), a real dump process and a real `compose up` process: state before the signal, the command gone, the lock free afterwards, the rollback), time limits (a pull or dump over its limit stops before any change, `compose up` over its limit fails with its rollback, the limits from the settings file and their ranges) |
| `test/deps_check_test.py` | `diun-deps.sh check` prints valid JSON with every field the plugin reads |
| `test/loading.js` | loading indicator, double loads, error cases |
| `test/i18n-deps.js` | DE/EN, requirements panel, install dialog |
| `test/update-backups.js` | update buttons, plan dialog, dump tick box, unknown jump size (warning, tick box), rollback offer, cause above the log, backups tab with causes (a failed rollback offered again, an aborted and a running update, the helper's own cause), output button, a build that failed before (row note, second tick box), “together with” in the rows, CI job containers left out, the warning for running CI jobs, plan groups and dependencies, delete |
| `test/diun-events.js` | which Diun event belongs to which row: its own image and tag only, none for a reference with a digest, no update for the same build under a new digest, the registry link from any event of the image, a dismissed event, the events-only view |
| `test/version-change.js` | “Change version”: rows with the button, outlined chips as buttons, the field's groups and locked entries (database, Nextcloud), preview (changed lines, plan, hints), tick boxes for a major and a floating tag, the last choice wins, refusals, `--expect` with the preview's fingerprint and `--no-dump`, failure with rollback, a stopped rollback, the history entry and its rollback dialog, no German in English; the states while the dry-run and the run work are checked at fixed points (the stub's answers wait for a gate) |
| `test/radar-cleanup.js` | “Run radar now”, cleanup box and “Clean up now”, marks, versions in the history and the update result |
| `test/version-cols.js` | columns Configured / Installed / Newest, chips, legend, not in a registry, a build after the newest release, the newest build of a registry without release tags, both build cells with stale data (marked, the error in the tooltip, no claim of a fresh check, gone after a good run), the revision of a development build, the bases of an image built here, a line tag refined by the radar, and kept with its patch when the registry failed |
| `test/page-frame.js` | header, tabs (mouse, keyboard, nothing loads), status labels with icons, buttons per place, dialog buttons, signature |
| `test/fonts_test.py` | every font family the stylesheets name (custom properties resolved, in the order `index.html` loads them) that has an `@font-face` has its file in `plugin/`: no request for a font outside the add-on; Red Hat Mono is the code font, from `RedHatMono.woff2`; counter-check that a font from Cockpit's files and a missing file are caught |
| `test/contrast_test.py` | WCAG contrast of every colour pair the page uses, light and dark (text 4.5:1, borders and icons 3:1) |
| `tools/check-design-files.sh` | the shared design files match `tools/design-files.sha256` |
| `test/harness.js` | prints the whole table (smoke test, also for looking at it) |
| `tools/check-version.sh` | version in `diun.js` = `?v=` in `index.html` = section in `CHANGELOG.md` |
| `tools/check-units.sh` | `systemd-analyze verify` for the units (CI lint step, next to shellcheck and ruff) |
| `test/e2e-server.sh` | real Docker host, throw-away projects `imgupd-test` (Postgres), `imgupd-test-my` (MariaDB, MySQL) and `imgupd-test-reg` (pulls against a local registry: nothing newer, registry down without and with a newer image here); since 0.19.0 every dump is restored into a fresh Postgres, MariaDB and MySQL container and compared object by object with the original (half a dump must be caught), the update lock is held by a separate process instead of a race, and CI runs it (job `e2e`, and before every release); since 0.11.0 also the tag change in the compose file, through `.env` and in an override file with their rollbacks, and the refusals for a shared variable and an anchor (the test puts a radar record for its local image into `versions.json` and removes it again); since 0.18.1 a rollback refused while the compose file names another image; since 0.20.0 `status.json` after the failed update (644, without the project folder), `status` as root and without root (`nobody`, German) with the failure, and without it after the rollback; at the end `status.json` is written again without the test project. `E2E_LIB=<folder>` runs helpers that are not installed yet. |
| `test/upgrade_base_test.py` | `tools/upgrade-base.py`, the release the CI upgrade test starts from: a build newer than the newest release, a build of a version released already (the release before it), no older release with packages (exit 3), Semantic Versioning precedence instead of text order, drafts, pre-releases and releases without packages left out |
| `test/package-upgrade.sh` | CI (job `upgrade`), in a throw-away Debian 13 or Fedora 43 container: the release from `tools/upgrade-base.sh` (checked against `SHA256SUMS` and its build provenance), data a server has, this build over it; the version went up by the package manager's own comparison, the package database names this build, the files on disk are this build's byte for byte (`dpkg --verify` and `diff` against the `.deb`, `rpm -V` and the digests of the `.rpm`), the data unchanged, removed again with the data kept. Without an older release it says so and checks a fresh install |
| `test/cockpit-setup.sh`, `test/cockpit-session.py` | CI (job `cockpit`), a real Cockpit with systemd and Docker on a throw-away GitHub runner, Playwright with Chromium through Cockpit's login: this build installed with apt over the release before, the page reloaded and logged in again after the upgrade (the new version, `i18n.js` of this build); the page in Cockpit's menu; since 0.21.0 right after each login the page loaded hidden for the menu and not started, and the menu's status icon (the shell's `sessionStorage` and the icon) as `diun-update.py status` gives it, after the login, after the radar runs, the roll-out and the rollback, and in German for the user without sudo before the page is opened; no JavaScript error and no failed request of the add-on's files or frame, and no other kind of error than Cockpit shows on its own (a login before the add-on is installed is the baseline; on Ubuntu 24.04 Cockpit 314 answers `*/po.js` and `*/po.manifest.js` with 404 on every page; the page's own request for Red Hat Mono, which that Cockpit does not ship, was caught this way; the page of the release before is checked only for loading, its errors are listed apart in the report); with administrative access (a sudo user) and without (a user without sudo, German browser: the hint, no actions); skopeo and the radar set up from the page (timer on, a first run), “Run radar now” (a new run); a throw-away compose project (a local registry, busybox 1.36.1 running, 1.37.0 pulled under the same tag): “Roll out image” with its plan, live output and result, the container on the new image, then the rollback from the history. Versions, steps and results in the job summary and the artifact `cockpit-session` (with screenshots) |

The fixtures in `test/fixtures/` come from a real host. Host names are removed; `make_v2.py` builds
`inventory3.jsonl` and `versions2.json` from `inventory2.jsonl`.
