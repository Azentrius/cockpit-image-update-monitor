# Internals: interfaces, formats, flows

Reference for everyone working on the code. The helpers are the **only** bridge between the browser and
root. Whoever changes a format changes helper, plugin, fixtures (`test/fixtures/`) and this document
together.

## Paths

| Path | Content | Owner / mode |
|---|---|---|
| `/usr/share/cockpit/image-updates/` | plugin (package `cockpit-image-update-monitor`, up to 0.12 `cockpit-image-updates`) | root, 644 |
| `/usr/lib/cockpit-image-updates/` | helpers (`*.py`, `*.sh` 755; `diunlib.py`, `diunnotes.py`, `diunproc.py`, `diunstatus.py` 644) | root |
| `/var/lib/diun-radar/versions.json` | result of the version radar | root, 644 (StateDirectory) |
| `/var/lib/diun-radar/releases.json` | release notes from GitHub for the update and version change dialogs (since 0.23.0, see [`releases.json`](#releasesjson-radar-since-0230)) | root, 644 |
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
| | `pull <container>` | action | text + `@@RESULT {…}` (since 0.23.0, step one of an update in two steps) |
| | `rebuild <container> --dry-run` | read (since 0.24.0; an image built on this server) | one JSON object: the `plan` of a rebuild, see [`rebuild`](#rebuild-an-image-built-on-this-server-since-0240) |
| | `rebuild <container> [--no-dump]` | action (since 0.24.0) | text + `@@RESULT {…}` |
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
| | `dbmajor <container> <tag> --dry-run` | read (since 0.23.0; stage 5, PostgreSQL, since 0.25.0 also MariaDB) | one JSON object: the `retag` plan plus `major`, see below |
| | `dbmajor <container> <tag> --expect <fingerprint>` | action (since 0.23.0) | text + `@@RESULT {…}` |
| | `delete-olddata <op-id>` | action (since 0.23.0) | text: releases the data a database major kept |
| | `status --lang en\|de` | read (since 0.20.0; the Status Board, also **without root**) | one JSON object, schema 1, see [`status`](#status-diun-updatepy-since-0200) |
| `diun-versions.py` | `--output FILE` (systemd), `--table`, without arguments JSON lines | timer / diagnosis | – (`--output` also writes `status.json` and, since 0.23.0, `releases.json` next to the file) |

Inputs are checked by regex:

| Input | Regex |
|---|---|
| container name | `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$` |
| operation ID | `^\d{8}-\d{6}-<name>$` |
| dump path | `^<name>/\d{8}-\d{6}_<name>\.sql\.gz$` + `realpath` check |
| rollback tag | `^rollback-\d{8}-\d{6}$` |
| target tag (`retag`, `dbmajor`) | `^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$`, and it must be one of the radar's `choices` |
| fingerprint (`retag --expect`, `dbmajor --expect`) | `^[0-9a-f]{64}$` |
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
| `source` | (since 0.23.0) the image's repository on GitHub, `{"repo": "owner/name", "via": "known|label|ghcr"}`, for the release notes (`diunnotes.source_of`, first match wins): `known` = a short list of checked images (`KNOWN`: official images on Docker Hub, and images whose label names the repository of their Dockerfile, `nextcloud` → `nextcloud/server`); `label` = `org.opencontainers.image.source` or `org.label-schema.vcs-url` on github.com (`.git`, `git@`, a path below the repository are fine); `ghcr` = `ghcr.io/<owner>/<name>`, whose owner is a GitHub account. Only names GitHub allows (account: letters, digits, single hyphens, 39 at most; repository: letters, digits, `.` `_` `-`, never `.` or `..`). Missing when none is found, and in data from older radars. |
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

### `releases.json` (radar, since 0.23.0)

The release notes of stage 3, written by the radar run (`diun-versions.py --output`) after
`versions.json` and `status.json`; `lib/diunnotes.py`. The page reads it when a dialog shows a plan;
the browser never talks to GitHub.

```json
{"generated": "2026-10-20T12:00:00Z",
 "repos": {
   "cube-js/cube": {"status": "ok", "checked": "…", "tried": "…", "floor": "1.6.74", "top": "1.8.8",
                    "more": false,
                    "releases": [{"tag": "v1.8.8", "version": "1.8.8", "name": "…", "published": "…",
                                  "url": "https://github.com/cube-js/cube/releases/tag/v1.8.8",
                                  "body": "## What's Changed …", "cut": true}]},
   "acme/gone": {"status": "missing", "checked": "…", "tried": "…"},
   "acme/app":  {"status": "limited", "tried": "…", "error": "HTTP 403", "checked": "…", "releases": […]}
 }}
```

- **Which sources:** those of the records (`source`) with something newer (`status` `patch`, `minor`
  or `major`) and a known installed version. Others are left out of the file.
- **Request:** `GET https://api.github.com/repos/<owner>/<name>/releases?per_page=100` without a
  login (60 requests per hour per address), 20 s per request, one after another. At most 20 requests
  per run (`MAX_REQUESTS`), none when 5 or fewer are left (`RESERVE`, from `X-RateLimit-Remaining`).
- **Asked again:** never within an hour of the last try (`RECHECK`; “Run radar now” clicked again);
  after 24 hours (`TTL`); sooner when the last try failed, or the radar now knows a newer version
  (`top`) or an older installed one (`floor`) than that answer had. `missing` waits the 24 hours.
- **Answers:** `ok`; `missing` on 404 (no such public repository); `limited` on 429, or 403 with no
  request left or `Retry-After` (GitHub's secondary limit): no further request this run; `error`
  otherwise (an HTTP status, or no network: no further request this run). On `limited` and `error`
  the last `releases` stay with their `checked` time.
- **Releases kept:** no drafts or pre-releases, no tags without a version (`stable`, `nightly-dev`)
  or with a pre-release word after it (`-rc.1`, `b7`). The version is the first `N.N…` in the tag;
  what comes before it (`v`, `release-`, `n8n@`) is its kind. In a repository with several kinds the
  one whose versions the images have counts (else the one with the most releases). Kept are those
  newer than the oldest installed version (`floor`), by version and not by date (redis and ClickHouse
  publish maintenance branches in between), newest first, 25 at most (`KEEP`); `more` when older ones
  in that range were left out or did not fit into the answer. `body` is the release's Markdown, cut at
  8000 characters (`cut`). `url` only on github.com; `name` only when it says more than the tag.
- **A problem never fails the radar run:** `versions.json` and `status.json` are written before; the
  error goes to the journal (`release notes not written: …`).

How the page shows it: see *Release notes* under [Plugin internals](#plugin-internals).

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
  `built-locally` (a service with `build:`; since 0.24.0 `rebuild` takes it), `compose-config-error`
  (+`detail`) or `service-missing` (+`missing`: services the compose files do not define, e.g. renamed
  since the start). The plan of a rebuild refuses the other way round: `not-built` when a service of
  the image has no `build:`.
- `build` (since 0.24.0): `true` in the plan of a rebuild, else `false`; missing in plans that stopped
  before the services were read.
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

### `dbmajor` (database major, stage 5, since 0.23.0)

A PostgreSQL major (16 → 17) by dump and restore, with the tag change of stage 2 for the tag line;
since 0.25.0 a MariaDB major (11 → 12) the same way (“MariaDB” below says where it differs).

`dbmajor <container> <tag> --dry-run` is `retag … --dry-run` (which refuses a database major,
`db-major`) for the database's new major tag from the radar's `choices`, plus `major`:

```json
"major": {"possible": true, "reason": null, "detail": null, "from_major": 16, "to_major": 17,
          "data": {"type": "volume", "name": "glitchtip_pgdata", "driver": "local",
                   "source": "/var/lib/docker/volumes/glitchtip_pgdata/_data",
                   "destination": "/var/lib/postgresql/data"},
          "pgdata": "/var/lib/postgresql/data", "stop": ["web", "worker"], "extensions": ["pgcrypto"],
          "data_bytes": 162529280, "data_free": 84654432256, "fingerprint": "…64 hex…"}
```

- `data`: the mount that holds the container's `PGDATA` (its environment, else
  `/var/lib/postgresql/data`; the longest destination that contains it). `pgdata`: where the new
  major will keep its data: the compose file's `PGDATA` for the service, else
  `/var/lib/postgresql/data`, from 18 on `/var/lib/postgresql/18/docker` (the image's default since
  18). It must lie in `data`, or the new major's data would not be in the volume.
- `stop`: the running services that wait for the database (`dependents`); they are stopped from the
  dump on, so that nothing is written that the dump lacks. `extensions`: those in use in any database
  (`pg_extension`, without `plpgsql`); the run checks that the new image ships them.
- `data_bytes`: the clusters' size (`pg_database_size`); `data_free`: free space where the data lies
  (the new cluster needs about as much again, as the old data stays).
- `fingerprint`: of the tag change's fingerprint, `data`, `pgdata` (MariaDB: `datadir`, and the
  engine) and `stop`.
- `engine` (since 0.25.0): `postgres` or `mariadb`. A container is MariaDB when it is a MySQL-type
  server (`kind_of`) and its image is called `…/mariadb` or sets `MARIADB_VERSION` / `MARIADB_MAJOR`.
  MariaDB: `datadir` is its data directory (`--datadir` in the command, else `/var/lib/mysql`), the
  mount is the one that holds it; `pgdata` is `null`, there are no extensions and no MD5 check;
  `data_bytes` is the size of the data directory (`du -sb`); the root password must be in the
  container's environment (`MARIADB_ROOT_PASSWORD` or `MYSQL_ROOT_PASSWORD`).
- `reason` when not possible: the tag change's own (`not-offered`, `anchors`, …), or
  `major-not-supported` (`detail`: the image; neither PostgreSQL nor MariaDB, e.g. MySQL; up to 0.24.x
  `major-not-postgres`), `major-root-password` (MariaDB without a root password in its environment),
  `major-several` (more than one
  container or service on the image: one database per run), `major-stopped`, `major-not-newer` (the
  target is no newer major, or unknown, e.g. a tag like `latest`), `major-no-volume` (`PGDATA` in no
  volume or bind mount), `major-volume-driver` (not the `local` driver), `major-anonymous-volume`,
  `major-bind-mountpoint` (a bind mount that is a mount point of its own: the data cannot move to a
  sibling by renaming), `major-pgdata` (`detail`: the new major's data directory and the mount; for 18
  add `PGDATA: /var/lib/postgresql/data` to the service's environment, or mount the volume at
  `/var/lib/postgresql`), `major-unreadable` (the extensions or the roles could not be read; since
  0.25.1 for MariaDB also the users, roles and grants, `detail`: the error),
  `major-space`; since 0.25.0 also `major-md5` (`detail`: roles that log in with an MD5 password
  hash; the images of PostgreSQL 14 and newer let clients in over the network with
  `scram-sha-256` only, so these roles would be locked out; not when the compose file sets
  `POSTGRES_HOST_AUTH_METHOD` to `md5`, `password` or `trust`) and `major-probe-space` (`detail`:
  free, needed, Docker's directory; only when that lies on another filesystem than the data: the
  trial run needs room for a copy there); since 0.25.1 `major-role-name` (`detail`:
  `image_updates_import_role`; MariaDB: a role or user has the name the restore gives its own role).
  pgvector images (`pg17`) have no version tags the radar can compare: no choices, `no-choices`.

`dbmajor <container> <tag> --expect <fingerprint>` plans again (exit 5 when anything differs), then:

1. pulls the new image (a failed pull goes on when it is on the server) and checks that it has the
   extensions (`ls "$(pg_config --sharedir)/extension"`, `docker run --network none`);
2. tags the running image as `<repo>:rollback-<stamp>`;
3. stops `stop` (`docker compose stop`);
4. dumps the database (`pg_dumpall`, as an update does) and counts every table of every database
   exactly (`count(*)`, `"database|schema.table|rows"`);
5. writes the tag line (stage 2: a byte-exact copy, `docker compose config`); the run is in the
   history from here on. Before that (since 0.24.1) the data's `PG_VERSION` must name the old major
   (`pg_version_file`, from the container's `PGDATA` relative to the mount), and (since 0.25.0) the
   **trial run** must pass: the dump restored into a throwaway container of the new image
   (`docker run -d --rm --network none`, label `image-updates.probe=<op-id>`, an anonymous volume,
   `POSTGRES_USER`, `POSTGRES_DB` and `PGDATA` as the real one, a random `POSTGRES_PASSWORD`, never
   the real one) and counted as in step 8. A probe that does not start, a restore error or another
   count stops the major here; the probe is removed in every case, and probes a killed run left
   behind are removed before a new one starts.
   Until here anything that ends the run takes back what it did and starts `stop` again (exit 5).
6. stops the database and moves its data aside, by renames on the same filesystem: a named volume's
   into a new volume `<volume>-pg<old>-<stamp>` (label `image-updates.op`), a bind mount's into a
   sibling directory `<path>.pg<old>-<stamp>` (owner and mode as the original). Since 0.24.1 the
   move is journaled (see “Moves of a major's data” below): the place, the entries and the move are
   in the history before the first entry moves. An entry that does not move puts back the others and
   removes the empty place; if putting back fails too, everything stays where it is and in the
   history, and the run fails (the rollback puts the data together). No data is deleted.
7. starts the new major on the empty directory (`up -d --no-deps --pull never`) and waits until it
   accepts connections over TCP (`pg_isready -h 127.0.0.1`, at most 300 s): while the image sets up
   a new cluster, its server listens on the socket only.
8. drops the database the image created on its first start (`POSTGRES_DB`, empty) and restores the
   dump with `psql` (`ON_ERROR_STOP=0`); any `ERROR:` line but the existing role of `POSTGRES_USER`
   fails the run (`reason.source: "restore"`). Then it counts again: any difference fails it
   (`reason.source: "counts"`, the first difference as `line`). Since 0.25.0 the user of the
   environment then logs in over TCP with `POSTGRES_PASSWORD`, as the waiting services do: a refused
   login fails the run (`reason.source: "login"`); without that variable the check is skipped.
   `vacuumdb --analyze-only` renews the statistics (a failure there is a note).
9. starts `stop` again and waits for them (`wait_healthy`).

From step 6 on a failure is a failed run (exit 4) with its rollback; `stop` stays stopped until it is
rolled back. `@@RESULT` adds `image`, `backup` and `old_data` (where the old data is kept).

**MariaDB** (since 0.25.0) runs the same steps with these differences. Every command in the container
logs in as root with the password of its environment through `MYSQL_PWD`, never on a command line;
the trial run gets that password through the environment of `docker run -e MARIADB_ROOT_PASSWORD`,
because the dump brings the old users with it.

- Dump: not the dump of an update. The system tables of the old major (the `mysql` database) do not
  load into a newer one (`mysql.proc` has another column count, found by the trial run), so the major
  dumps the users' databases (`--databases`, all but `mysql`, `information_schema`,
  `performance_schema`, `sys`; `--single-transaction --routines --events --triggers`) and after
  them the accounts as statements (`MY_MAJOR_DUMP`), since 0.25.1 in an order the new server takes:
  every user (`CREATE OR REPLACE USER`, from `SHOW CREATE USER`); `CREATE ROLE
  image_updates_import_role` and `SET ROLE` to it; every role (`CREATE OR REPLACE ROLE … WITH ADMIN
  image_updates_import_role`, also a role nobody has; not `PUBLIC`, which every server has); the
  grants of every user, role and of `PUBLIC` (`SHOW GRANTS` without its `SET DEFAULT ROLE` lines:
  privileges, roles granted to users and roles, `WITH GRANT OPTION`, `WITH ADMIN OPTION`); `SET
  DEFAULT ROLE … FOR …` from `mysql.user.default_role`; `SET ROLE NONE`, `DROP ROLE
  image_updates_import_role` (which takes its admin rights with it, so root keeps exactly the roles
  it had), `FLUSH PRIVILEGES`. The restore's own role is the admin of the roles while it grants
  them, as with `mariadb-dump --system=users`. Left out: `mariadb.sys` and `healthcheck` (the image
  sets up its own, with the password in the data directory). Every list and every `SHOW` is read
  whole before a pipe uses it: an error ends the dump without the end marker (up to 0.25.0 a pipe
  to `sed` hid it, and roles were not taken along at all). Names reach the server as literals of
  `QUOTE()` only. Database names with spaces are not supported.
- Accounts (since 0.25.1): `MY_GRANTS` lists every user, role and `PUBLIC` the dump takes along
  with its `SHOW GRANTS` lines (`"account<TAB>line"`), read before the dump, in the trial run after
  `mariadb-upgrade` and after the restore and the restart (`account_grants`). Every account of the
  old server must be there with the same lines (`compare_grants`); an account only the new server
  has (one its image sets up from the environment) does not count. A difference stops the trial
  run (exit 5, “privileges differ: …”) or fails the run (`reason.source: "grants"`).
- Counts: every base table of every database but `mysql`, `information_schema`, `performance_schema`
  and `sys`, `count(*)` each (`"db<TAB>table<TAB>rows"`).
- Before anything changes the server must say the old major (`select version()`), and so must
  `mysql_upgrade_info` in the data directory if there is one (then recorded as `version_file`).
- Ready: `mariadb-admin ping --protocol=tcp -h 127.0.0.1` (the server the image runs while it sets up
  a new data directory has no network).
- Restore: the client reads the dump and stops at the first error (`reason.source: "restore"`). Then
  `FLUSH PRIVILEGES` and `mariadb-upgrade --force`, because a restore leaves the system tables of the
  dump's version while `mysql_upgrade_info` already names the new one (`reason.source: "upgrade"` if
  that fails), then `docker compose restart` and ready again; the trial run does the same up to the
  upgrade. Then the counts and the login: `MARIADB_USER` / `MARIADB_PASSWORD` (or `MYSQL_…`) over
  TCP, root without an app user.
- The places are called `<volume>-mariadb<old>-<stamp>` and `<path>.mariadb<old>-<stamp>`.

#### Moves of a major's data (since 0.24.1)

Every entry of a directory moves into another by `rename` on the same filesystem; nothing is copied,
and each entry is always in one of the two places. A move is in the history (`operations.jsonl`, each
line `fsync`ed) before its first entry moves, as `"move": {"step", "from", "to"}`, and set to `null`
after its last; signals of `on_signals` wait meanwhile (`held`), so a SIGTERM, SIGHUP or SIGINT takes
effect only once the move, the places and the history agree. Steps:

- `aside`: the major, the old data into its place (`old_data`, with `data_entries`, the names of the
  entries, recorded at the same time);
- `new-aside`: the rollback, the new major's data into a place of its own (`new_data`), only if the
  directory is not empty;
- `old-back`: the rollback, the old data back.

A move refuses to start when the target has a name the source has (a rename would replace a file:
exit 5 for the rollback, “unclear state”). A place is removed only while it is empty (`drop_empty`);
only `delete-olddata` deletes data, and not while a move is open (exit 3; `backups` gives
`can_release: false` then).

The end of a move comes in three steps (since 0.25.1): the history says the move is over and which
place it emptied (`move: null`, `old_data` or `new_data` `null`, `emptied`: the place), then the
place is removed if it is empty, then `emptied: null` (`drop_emptied`). A rollback first removes a
place an earlier run left in `emptied`; one with data in it stays, and so does its record. Up to
0.25.0 the place was removed first, and a kill right after that left the history naming the move
and a place that was gone: the rollback stopped with exit 2 (`old_data`) or exit 5 (`new_data`),
and the database stayed stopped. The rollback of 0.25.1 recognises that state (`old_data_home`):
the place of `old_data` gone, the history naming a move `old-back` into the data directory or
`aside` out of it (a move aside taken back), `data_entries` recorded, no `data_released`, and the
data directory holding exactly those entries and the old major's version file (`data_restored`).
Each entry moves whole by one rename, so with all of them there nothing is missing. It records
`old_data: null` and goes on; anything else stops it as before (exit 2, nothing moved, no database
started). For `new_data` (a move of the new major's data taken back): its place gone and the data
directory not empty, the move is over, and the data gets a new place. `backups` offers that
rollback again.

After a SIGKILL, a power cut or a failed undo the history keeps the open move, and the operation
stays `running` or `failed` with its rollback. The rollback (`swap_back`) finishes it: an open
`aside` puts the rest of the old data back (the new major never ran), an open `new-aside` or
`old-back` goes on where it stopped. A run of 0.23.0 to 0.24.0 recorded the place only after the
move; a signal or a kill in between left `old_data: null` with the data in the place. The rollback
finds such a place by the name that version gave it (`<volume>-pg<old>-<stamp>` with the label of
the operation, `<path>.pg<old>-<stamp>`) when it holds data, records it and puts it back.

Before the rollback starts the old major, the data must be its own again (`data_restored`): exactly
the entries recorded in `data_entries` and `PG_VERSION` (`pg_version_file`) of the old major (MariaDB:
`mysql_upgrade_info`, `version_file`, if it was recorded); for
older operations a directory that is not empty and, if there is a `PG_VERSION` at its top, that of
the old major. Otherwise the rollback fails (exit 4) without starting the database (on an empty
directory PostgreSQL would set up a new, empty cluster) and leaves everything where it is.

### `rollback <op-id>`

Takes the update lock. Before anything changes (otherwise exit 2 / 3 / 5, nothing touched):

- the operation is an `update`, `retag`, `dbmajor` (since 0.23.0) or `rebuild` (since 0.24.0) that
  is not rolled back yet, or whose rollback failed: a failed rollback may be tried again (since
  0.18.1; up to 0.18.0 it was marked `rolled_back` too and refused with exit 3);
- the rollback image exists;
- the files of a tag change (above);
- since 0.18.1 `docker compose config` gives every service of the operation its `image`, for a tag
  change once its copies are back (if not, the files are written again as they were before the
  attempt). Otherwise compose would start another image than the rollback copy, which only takes
  that tag back: an `image:` line changed after the update (`acme/app:latest` → `acme/app:2`), a
  `.env` edited, a service removed. Exit 5 names the services and the images they get now. A
  service with `build:` and no `image:` has no `image` in that output; since 0.24.1 it gets the name
  Compose v2 gives the image it builds, `<project>-<service>`, in the project compose runs it in
  (the output's `name`, from `-p`): up to 0.24.0 the rollback of such a rebuild was refused.

Then the tag points to the rollback image, `up -d --no-deps --pull never`, waiting for health, and
since 0.18.1 every container of the services (one-off `compose run` containers aside) must run the
rollback image's ID: one that runs another image fails the rollback (exit 4, `reason.source`
`"image"`). A database major first moves the data back and checks it (“Moves of a major's data”).

### `pull` (update in two steps, step one)

`pull <container>` takes the update lock and the plan (`make_plan`, so the same services and the same
refusals as `update`, exit 2), then runs `docker compose pull <services>` with `DOCKER_TIMEOUT` and
changes nothing that runs: no dump, no rollback copy, no history entry, no container recreated. The
row then shows “Roll-out pending”, and `update` later rolls the image out (its plan says `pulled`).
A failed pull, a time limit or a signal is exit 5 (“nothing changed”); no newer image is `nochange`.
`status.json` is written again afterwards, so the Status Board counts the roll-out pending.

### `rebuild` (an image built on this server, since 0.24.0)

For a compose service with `build:`, which `update` refuses (`built-locally`): the image is built again
from its Dockerfile with fresh base images. `rebuild <container> --dry-run` is the plan (`make_plan`
with `build`: the same services, relations, databases and refusals, plus `not-built`), with
`build: true`, no `jump_unknown` (the Dockerfile is the same) and the versions of the final base:
`installed_version` the version the running build carries (its variable, the base named by the
radar's `base`), `target_version` what that base's tag points to now (`now`), `jump` between them.

`rebuild <container> [--no-dump]` runs `update`'s steps with one difference: step 3 is
`docker compose build --pull <services>` instead of `docker compose pull`. A build that fails, does
not end within `DOCKER_TIMEOUT` or is stopped by a signal changes nothing that runs (exit 5); the run
is taken back like a failed pull (dumps, rollback copy), and the image tag goes back to the build it
named before, should a build of several services have moved it. A build that gives the image running
now is `nochange`. The history entry has `kind: "rebuild"`; `from_version` / `to_version` are the
version of the image itself or, without one, that of its final base. `rollback` and `backups` treat it
as an update.

### `@@RESULT` (last line of `update` / `pull` / `rollback` / `cleanup`)

```
update:   @@RESULT {"op": "20261012-015025-imgupd-test-db", "status": "ok|nochange|failed",
                    "rollback": "repo:rollback-20261012-015025", "dumps": ["project/…sql.gz"],
                    "from_version": "16.14", "to_version": "16.15"}
          (on failed also "reason": {…}, see below)
rebuild:  as update (since 0.24.0)
retag:    as update, status "ok|failed", plus "image": "repo:<new tag>" and
          "backup": "/var/backups/image-updates/compose/<project>/<stamp>"
rollback: @@RESULT {"op": "…", "status": "ok|failed"}            (on failed also "reason")
pull:     @@RESULT {"status": "pulled|nochange", "image": "repo:tag",
                    "from_version": "16.14", "to_version": "16.15"}   (no "op": nothing recorded)
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
    "detail": "ollama, prod-mcp-1", "link": "#/updates/update", "checked_at": "2026-10-12T02:50:00Z",
    "stale_after": 86400},
   {"id": "failed", "state": "ok", "title": "Failed updates", "value": "0", "link": "#/backups/history",
    "checked_at": "2026-10-12T02:50:00Z", "stale_after": 86400},
   {"id": "radar", "state": "ok", "title": "Version radar", "value": "< 1 h", "link": "#/updates",
    "checked_at": "2026-10-12T02:17:34Z", "stale_after": 86400},
   {"id": "rollout", "state": "info", "title": "Roll-out pending", "value": "3",
    "detail": "docuseal-db, glitchtip-postgres, plausible_db", "link": "#/updates/rollout",
    "checked_at": "2026-10-12T02:50:00Z", "stale_after": 86400},
   {"id": "new-versions", "state": "info", "title": "New versions", "value": "17",
    "detail": "actual-ai, actual_server, calcom-db, diun-socket-proxy, docuseal +12",
    "link": "#/updates/new-versions", "checked_at": "2026-10-12T02:17:34Z", "stale_after": 86400}]}
```

| Item | State | Counts |
|---|---|---|
| `updates` | `info` with containers, else `ok`; `none` while Diun has written no event | the rows the page marks “Update available”: Diun's newest event of the image and tag names another digest than the one running, not dismissed and not the same build (`sameBuild`) |
| `failed` | `danger` with entries, else `ok` | the `update` and `retag` runs of the history that failed and were not rolled back, whose rollback failed, or that ended without their last line (`running` while no process runs it: aborted), as long as no later run settled them. A run stands for what `make_plan` grouped: its `services` in its compose project and compose files (`workdir`, `files`), and the containers it names. A later run (any outcome, also one still in progress) settles only those services in the same files (or where the files are not recorded) and the containers it names: a success of another service, another tag of the image or the same image in other compose files settles nothing; a later run of the same services, also a tag change, does. A run partly settled is named by a container still affected, with the services still affected. Entries without `services` (older versions) stand for the containers they name, and they settle no services |
| `radar` | `ok`; `warning` when `versions.json` is older than 24 hours (the page's “outdated”) or its time is unreadable, or (since 0.22.0) when its last run reached no registry (`no_registry`; title “Version radar reached no registry”, the detail points to DNS and the network, never the error itself, which can name addresses); `none` without `versions.json` | `value` is the age (“2 h”, “3 days”) |
| `rollout` | `info` with containers, else `ok` | the rows “Roll-out pending” (`pulled`) |
| `new-versions` | `info` with containers, else `ok`; `none` without `versions.json` | the rows “New version”: a newer patch or minor, a major of an image that is no database, or a newer base of an image built here |

- **The page's rules.** `updates`, `rollout` and `new-versions` are the rows of the page's status
  column (`rowFor`, `versionCell`, `installedOf` in `overview.js`, ported to `diunstatus.py`); CI job
  containers are no rows. `test/status-rows.js` checks with the fixtures that the page shows exactly
  the rows `status.json` names. A rule that changes on the page changes there as well.
- **State and summary** as the contract says: the gravest of `danger` > `warning` > `info`, otherwise
  `ok` when an item is `ok`, otherwise `none`. The summary names the findings, the gravest first (“1
  failed update, version radar outdated, 2 updates available”), or says “No updates or problems
  found”.
- `detail`: at most five names, then “+N”; `value`: a number as text, the radar's age.
  `stale_after` is 86400 on every item.
- `link` (since 0.23.0, `LINKS` in `diunstatus.py`), in every state: `updates` `#/updates/update`,
  `rollout` `#/updates/rollout`, `new-versions` `#/updates/new-versions`, `failed`
  `#/backups/history`, `radar` `#/updates`. The Status Board jumps to the link of its key figure
  (`cockpit.jump("/image-updates#/…")`); the page shows that place, see
  [Plugin internals](#plugin-internals), “Places”.
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
- **Rebuild** (since 0.24.0): `kind: "rebuild"`, the fields of an update; the versions are those of
  the final base when the image names none of its own.
- **Database major** (since 0.23.0): `kind: "dbmajor"`, the fields of a tag change plus `from_major`,
  `to_major`, `data` (the mount), `stopped` (the services stopped), `old_data` (where the old data is
  kept: `{"type": "volume", "name", "path"}` or `{"type": "bind", "path"}`). A rollback adds
  `new_data` (the new major's data, kept) and sets `old_data` to `null` once the data is back;
  `delete-olddata` adds `data_released`. Since 0.24.1 also `pg_version_file` (the old major's
  `PG_VERSION`, relative to the mount), `data_entries` (the names of the entries set aside) and
  `move` (an open move, or `null`); see “Moves of a major's data”. Since 0.25.0 `engine`
  (`postgres` or `mariadb`; missing means PostgreSQL) and `version_file` (MariaDB's
  `mysql_upgrade_info`, relative to the mount, if there was one). Since 0.25.1 `emptied` (a place
  whose move is over and that is to be removed, see “Moves of a major's data”). `backups` leaves
  these out except `engine`.
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
(whether output is kept) added and without paths, image IDs and `pid`. A database major (since
0.23.0) has `kept` while the data it keeps is there: `{"where", "bytes", "major", "can_release"}`
(the old major's after a run not rolled back, the new major's after a rollback; releasing is possible
after a good run or a rollback, `delete-olddata`). `rollback_gone: true` (since
0.22.0) marks a run that could be rolled back but whose rollback image no longer exists (deleted by
the cleanup, with *Delete* or outside the add-on, e.g. `docker image prune -a`); the history then says
“Rollback copy gone” with the image in the tooltip instead of only leaving the rollback button out.
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
- **Scripts** (since 0.23.0, before that all in `diun.js`): classic scripts that share
  `window.ImageUpdatesPage`, loaded by `index.html` in this order after `i18n.js` and `menu-status.js`:
  `common.js` (paths, `t()`, `icon()`, formatting, labels, alerts, buttons, `spawnHelper`; it opens
  the namespace), `dialog.js` (the action dialog, `confirmAndRun`, `ticket()` for answers that come
  later), `notes.js` (release notes), `update.js` (plan, update in two steps, rollback), `retag.js`
  (“Change version”), `overview.js` (the Updates tab: parsing, rows, columns, status, meta line),
  `backups.js` (the Backups tab and the cleanup) and `diun.js` (the version, requirements, “Run radar
  now”, tabs, places, loading, the start). Each script takes the building blocks of `common.js` and
  `dialog.js` when it loads and calls the others through the namespace when it runs; shared state
  is read through small functions (`isDismissed`, `rendered`, `backupsState`, `deps`). No ES modules:
  the jsdom tests evaluate the scripts as the browser runs them (`test/page-scripts.js` takes their
  list from `index.html`), and jsdom runs no modules.
- **All parts loaded?** (since 0.23.0) Cockpit's bridge keeps the list of a package's files it read
  first in a session; a file an update brings new answers 404 until the bridge reads the packages
  again. The shell asks for that when it starts (`ReloadHint`) but loads the page at the same time,
  so after an update and a reload of the browser a script can be missing (Cockpit 314 in CI). `diun.js`
  checks a name of every script in the namespace (`PARTS`); with one missing it calls
  `cockpit.Packages.Reload` on the internal bus and loads the page again, once per version and browser
  session (`sessionStorage`); if a part is still missing then, `#dm-status` says so (texts from
  `i18n.js`, as `common.js` may be the one missing). The other scripts return at once without
  `common.js`, so none throws.
- **Shared design files:** `azentrius.css` (tokens for light and dark, page frame, buttons, cards,
  tables, labels, dialogs), `azentrius-theme.js` (sets `pf-v6-theme-dark` on `<html>` from Cockpit's
  style, `shell:style`), `icons.js` (`window.AzIcons`), the fonts and `OFL.txt`. They are copies of
  `src/` in Azentrius/cockpit-cronjobs, pinned in `tools/design-files.sha256` and never edited here;
  `diun.css` holds only what this page needs on top. Icons are inline SVG from `icon(name)` in
  `common.js`, all from the shared set.
- **Fonts:** DM Sans, DM Serif Display and Red Hat Mono (code and versions) ship with the add-on.
  The page requests no font from Cockpit: its static files differ between Cockpit builds (Ubuntu
  24.04's Cockpit 314 has no Red Hat Mono and answered 404 when `azentrius.css` took it from there).
  `test/fonts_test.py` checks that every font the page uses ships with it.
- **Frame:** header (`az-page-header`, the only `az-btn--primary` is Refresh), tabs (`az-tabs`,
  WAI-ARIA keyboard), `#dm-status` and `#dm-deps` under the tabs, the panels, the signature (version,
  host from `/etc/hostname`, both written when the page starts). Both tabs are loaded together with
  every load; switching loads nothing.
- **Places** (since 0.23.0, `PLACES` in `diun.js`): the links of the status lead to a tab and a part
  of it, `cockpit.location.path`:

  | Path | Shows |
  |---|---|
  | (none) | the Updates tab |
  | `updates` | the Updates tab (the radar's item: its header and the requirements are at the top) |
  | `updates/update`, `updates/rollout`, `updates/new-versions` | the Updates tab, scrolled to its first row “Update available”, “Roll-out pending” or “New version” (`tr[data-kind]`: `update`, `pulled`, `newver`) |
  | `backups`, `backups/history` | the Backups tab; with `history` scrolled to “Update history” (to the error, when the backups could not be loaded) |

  A path that is no place changes nothing; a part that is none shows only the tab. Cockpit keeps a
  loaded page and only changes its location when the Status Board jumps to it again, so the page
  listens to `locationchanged`: every location shows its tab at once and scrolls to its part once,
  when the rows (or the history) are there and the page is shown (`cockpit.hidden` false, again on
  `visibilitychange`: the page may be loaded hidden for the menu). A reload scrolls nowhere. A tab
  chosen on the page (mouse or keyboard) goes into the location with `cockpit.location.go([tab])`
  unless the location names it already, so the link to the place shown before is a change again.
- **Status labels:** update / roll-out pending / new version `az-label--info` + `info-circle`, up to
  date `az-label--success` + `check-circle`, unknown `az-label` + `ban`. History results: successful
  success, failed danger, aborted warning, no change and rolled back neutral.
- **Version:** `const VERSION` in `diun.js`; `index.html` loads every script and stylesheet of the
  add-on with `?v=<version>`. `tools/check-version.sh` keeps them and `CHANGELOG.md` in step.
- **Manifest:** besides the menu entry, `azentrius.status` names the status command for the Status
  Board (see [`status`](#status-diun-updatepy-since-0200)); Cockpit ignores the section. `"preload":
  ["index"]` (since 0.21.0) loads the page hidden after the login, for the status icon in the menu
  (see [Status in Cockpit's menu](#status-in-cockpits-menu-since-0210)).
- **Language:** `../base1/po.js` sets `cockpit.language`. `de*` → German, otherwise English.
  - Texts live in `I18N.en` / `I18N.de` in `i18n.js` (since 0.19.0; `window.ImageUpdatesI18n`, loaded
    before `menu-status.js` and the page's scripts, which keep only the code). Static texts in the HTML carry
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
- **Database major** (since 0.23.0, stage 5): in “Change version” a PostgreSQL image's (name ending
  in `postgres`, `postgresql`, `postgis`, `pgvector`, `timescaledb…`) newer majors with a known target
  can be chosen (`data-major`; a tag that follows every major stays locked). Such a choice is planned
  by `dbmajor … --dry-run`: refused with the reason (`reason.major-*`, `major-pgdata` with what to add),
  or shown as a warning “Database major: PostgreSQL 16 → 17” with two tick boxes (a backup elsewhere
  checked, the downtime) before the plan list (data, new data directory, stopped meanwhile,
  extensions, size), then the changed line and the plan of an update without its dump tick box (a
  major always dumps). The button reads “Run database major” and runs `dbmajor … --expect
  <fingerprint>`; the result names where the old data is kept, a failure offers the rollback. The
  history names a major (“Database major: PostgreSQL 16 → 17”) and its kept data (`kept`), with
  *Release* (trash, `delete-olddata`, after a confirmation); `backups` offers no rollback once the old
  data is released.
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
  - History: kind `rebuild` gets “Rebuilt on this server” under the image (since 0.24.0).
  - History: kind `retag` gets “Tag changed: <from_tag> → <to_tag>” under the image; its rollback
    dialog names the files it puts back (`changes[].file`). A rollback that stops with exit 2 or 5
    says that nothing changed. Failed builds count only kind `update`.
- **Rebuild** (since 0.24.0): a row of an image built on this server (`rec.base`) whose base is newer
  (the base cell, `baseChecked` and `news`) and that compose runs (`managed`) gets *Rebuild* in the
  update's place (`data-update`, `data-kind="rebuild"`); the base cell's tooltip names it. The dialog is
  the update's (`startUpdate`) with the plan of `rebuild <c> --dry-run`: first “Base images” from
  `rec.base` (newer with `now`, unchanged, pinned, not checked), then `planBody`, without the two
  steps, without release notes and without the mark of a failed build. Confirming runs
  `rebuild <c>` (`--no-dump` without the dump box); the outcome has texts of its own, exit 4 offers
  the rollback. The Installed column of such a row (since 0.24.0) shows the version of the final base
  that carries one (`2.11.4 base caddy:2`) when the image names none; only shown, the status follows
  the bases.
- **Release notes** (since 0.23.0): the plan of an update and the preview of a version change have
  a slot after the plan list and the dump tick box (before the warnings below it and the choice of
  steps); `releases.json` is read when the plan is shown, and a slot of a plan shown again in the
  meantime is left alone. Shown are the releases of the row's `source` newer than `installed_version`
  and up to `target_version` (versions compared by their numbers, trailing zeros do not count; without
  a target, a tag like `latest` that names none, all since the installed version, “Release notes after
  1.6.74”): `<details>` closed, “Release notes 1.6.74 → 1.8.8: 3 releases” and, when any text mentions
  *breaking*, *migration* or *deprecated*, a warning label with the words; per release its tag, name,
  date and words, its text and “On GitHub” (a new tab, `noopener noreferrer`; only github.com links).
  The text is text: escaped first, then headings bold, list marks as bullets, links as their text,
  images, HTML elements and comments left out of the escaped text (so nothing left over can become
  HTML; `<script>` stays visible text), the words marked (`.dm-flag`). Below it the source and the time of its answer, and whether older releases are
  missing. Otherwise one note, never an error: no source, the installed version unknown, not fetched
  yet (also without `releases.json`), no releases, GitHub's limit, an error, no release in the range.
  Nothing for the same version (`jump` `same`).
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
  the state as `status` does, from the file's `facts`: the items' counts, the radar outdated after
  24 hours as of now, checked again the moment it turns 24 hours old, and (since 0.23.0) a radar run
  that reached no registry (`facts.radar.reached` false; outdated comes first). Two differences: the radar's
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
| `test/major_test.py` | the database major's plan and every reason it refuses (Docker and compose stood in for), where a new major keeps its data (18 with and without `PGDATA`, a volume at `/var/lib/postgresql`), counts compared, the error lines of a restore, the parts that move data (a place next to it, a move refused before its first rename when a name is in both places, a place removed only while empty), which kept places may be deleted, the refusals of `delete-olddata` (also while a move is open); since 0.25.0 roles with an MD5 password (refused, named; allowed when the compose file keeps MD5), room for the trial run in Docker's directory, and the trial run itself (no network, the label, the real one's user, database and PGDATA, never its password, an earlier probe removed first, removed afterwards; a restore error, other counts or a probe that does not start stop it with exit 5); the plan of a MariaDB major (by image name or by its version variable alone, `/var/lib/mysql` or `--datadir`, no extensions, the size, the root password required, MySQL refused, its own fingerprint), MariaDB's counts and `lead_major`; since 0.25.1 the plan of a MariaDB major refused when the users, roles and grants cannot be read (`major-unreadable`, the error named) or a role has the restore's own role name (`major-role-name`), and the grants read in the trial run after `mariadb-upgrade` (a role lost: exit 5, the first difference named) |
| `test/major_recovery_test.py` | since 0.25.1 also SIGKILL once a move is over: right after its emptied place is removed and after the history says so, before it goes (the child stops there too: after a place is removed, after a history line), for the move back, a move aside taken back and a move of the new major's data taken back, each with a bind mount and a volume, and MariaDB; the next rollback goes on, starts the old database once and on all of its data only, and leaves nothing open in the history; an emptied place with data in it again stays. The states 0.24.1 to 0.25.0 left (the same kills, the empty place then removed by hand) are finished; eight that are no move back done are refused (exit 2, nothing moved, no database started): an entry moved away, a file deleted (a volume), `PG_VERSION` of the new major, an entry more, the entries not in the history, MariaDB's version file of another major, no move open, a move of the new major's data open. MariaDB's grants read before the dump, in the trial run and after the restore; a role missing in the trial run (exit 5, nothing changed) or after the restore (exit 4, `reason.source: "grants"`, then the rollback). Since 0.25.0 also MariaDB 11 to 12 (a volume and a bind mount: the place named `mariadb11`, the version file recorded, the trial run's root password through the environment of `docker run` only, the privileges and `mariadb-upgrade` after the restore, a restart before the counts, the rollback; a version file of another major in the kept data starts no database) and a trial run that fails (exit 5, the database never stopped, nothing moved, nothing in the history, the waiting service started again, the probe removed); since 0.24.1: the major and its rollback in a process of their own (`test/major_child.py`: Docker and compose stood in for, the data in a temporary directory, a bind mount and a volume), stopped by real signals at a chosen rename (the child waits there until the signal is sent): SIGTERM after the first and after the last rename of the move aside, SIGTERM while the rollback moves the new major's data aside and the old data back, SIGKILL in the middle of either move with the next call finishing it, a failed rename and a failed undo (no file deleted), the major and the rollback without a break, the data of a 0.23.0 run in its place but not in the history, an empty data directory and a new empty cluster next to the kept data (no database started). Every file of the data is compared with its content; at each start of the database the data directory as it was. |
| `test/mariadb_dump_test.py` | since 0.25.1: the account part of a MariaDB major's dump (`MY_MAJOR_DUMP`) and the grants list (`MY_GRANTS`) run with `sh` against `test/fake_mariadb.py` (the client and `mariadb-dump`, answering from a server's accounts as MariaDB does: `QUOTE()`, `SHOW CREATE USER`, `SHOW GRANTS`, errors on demand). The SQL is played on a model of MariaDB's accounts that refuses a statement naming a user or role before it exists, or granting a role without the admin option, and must end with what the server had and nothing more: a user with rights only through a role and that role as default, a role that has another, a role nobody has with rights of its own, a user with direct rights, a role granted with the admin option, roles root holds, a name with a quote, `PUBLIC`; `healthcheck` and `mariadb.sys` left out, the restore's own role gone again. An error reading the databases, the users, the roles, the default roles, a user, the grants of a user, a role or `PUBLIC` ends the dump without the end marker. `account_grants` and `compare_grants` (a role gone, a default role lost, a role more for root; an account only the new server has does not count) |
| `test/db-major.js` | the database major in the page: PostgreSQL majors with a target selectable, a tag that follows every major locked, 18 refused with what to change, the plan of a major (data, stopped services, extensions, no dump tick box), all tick boxes before the button, the command with the fingerprint, the result; the release from the history; German and English; since 0.25.0 the refusals for roles with an MD5 password and for too little room for the trial run, in both languages, and a MariaDB major (its majors offered, MySQL's locked, the plan with the engine and the data directory and without extensions, the run, the history naming the engine) |
| `test/notes_test.py` | `diunnotes.py` with a fake GitHub (a request to the real one fails it): the source (known, also against a label, the labels in their forms, the second image of a tag, ghcr.io, other hosts, names GitHub would not take), versions of interest per record and per source, releases of an answer (drafts, pre-releases, tags without a version, cut text, links, kinds of tags), which of them (by version not by date, the image's kind of tag, newer than installed, `KEEP` and `more`), when a source is asked again (`RECHECK`, `TTL`, a newer or older version, `missing`, errors), GitHub's limit (403 none left, 403 `Retry-After`, 429: no further request, the last answer kept), a server error, no network, `RESERVE`, `MAX_REQUESTS`; the radar run writing `source` and `releases.json` (644), GitHub down (the last releases kept), a broken module or label not failing the run |
| `test/inv_test.py` | inventory with fake Docker, compose files per container, CI job containers, build time, source revision |
| `test/status_test.py` | `diunstatus.py` and `diun-update.py status`: the page's rules for the rows (Diun's event of the tag, `docker.io/library/`, another tag, a digest reference, dismissed, the same build in Docker's and Diun's time, a date before 1980, the newest event, roll-out first, CI jobs, no digest, a newer patch, a database major, `by_tag`, an empty comparison, a line tag refined by the radar, the base of an image built here), failed updates from the history (rolled back, a failed rollback also as written up to 0.18.0, aborted, still running, `discarded`; which later run settles which: another service and tag of the same project, the same image in other compose files or another directory, a retry and a tag change of the same services, overlapping groups, rollbacks and aborted runs next to an unrelated success, entries without services; the resulting state, summary and detail), the radar's age, state and summary in English and German with plurals, the contract's shape, no German in English, the fixture `status.json` of real host data, `status` without the file, with an unreadable one, from the file in German, the radar's age judged when asked, the history as root and the file without root, Docker not answering (the last file stays), `events.jsonl` as a link or a pipe and a large one, the arguments (exit 64, no root needed), the write after `rollback` and `cleanup` (silent) and none after `--dry-run`, a wrong call or without root, the radar run writing it and not failing for it; the `link` of every item in every state and language, and a link the Status Board would not take caught by the shape check |
| `test/status-rows.js` | the page with the fixtures of a real host marks exactly the containers “Update available”, “Roll-out pending” and “New version” that `test/fixtures/status.json` names |
| `test/menu-status.js` | the status icon in Cockpit's menu with `i18n.js`, `menu-status.js` and the page's scripts: preloaded and hidden, the icon from `status.json` (`superuser: "try"`) and nothing else read or run, shown, the page loads (once); opened directly; German; a changed file, the same again (not sent), a missing and an unreadable file; the radar turning outdated while the session stays open (the check at 24 hours); no answer at all (the page starts after 3 seconds); state and tooltip equal state and summary of `diunstatus.document()` for the same facts, in English and German (the fixture, failed updates with an outdated radar, singular forms, nothing to do, no Diun and no radar, an unreadable radar time, a time with an offset, a run that reached no registry, also outdated and next to a failed update, odd values) |
| `test/places.js` | where the Status Board's links lead, with `i18n.js`, `menu-status.js` and the page's scripts: each place opened directly (its tab at once, its first row once the rows are there, the history once it is there), while the page stays loaded (`locationchanged`), a tab chosen with the mouse or the keyboard going into the location and the link back to the place before, a reload that scrolls nowhere, no place, paths that are no place (also `constructor`, `toString`), no row of that status, the page loaded hidden (the location before and after the shell's hint, loaded at a place), and every link of `test/fixtures/status.json`: the Status Board's rule, its place, the rows its item counts |
| `test/proc_test.py` | `diunproc` with real child processes: a silent hang, endless output, children that ignore SIGTERM (the group killed), a dump that hangs, the next command after a timeout; signals: no handler changed on import, `on_signals` restores the handlers before it, `held` defers a signal; a helper that holds a lock gets SIGTERM or SIGHUP while its command runs (silent and printing, `stream` and `copy`) and keeps the lock until the stop has reached the command (synchronised through FIFOs), children that ignore SIGTERM with SIGTERM and SIGHUP again during the cleanup (SIGKILL after the grace time, repeats only counted), the next operation afterwards, SIGKILL (the command runs on and keeps the lock until it ends) |
| `test/update_test.py` | `diun-update.py` with fake Docker, compose and systemctl: database detection, plan reasons, services of the same compose files only, running CI jobs in a runner's plan, unknown jump size, argument checks and exit codes, update (dumps, rollback tag, versions, cleanup, radar start, a failed pull that leaves the old image, a registry error with and without a newer image on the server), rebuild (the plan with the versions of the final base, read-only, `not-built`, a failing build putting the tag back and leaving no history, the same build, a rebuild with its versions, history and rollback, a build over its time limit, since 0.24.1 a build without `image:` (compose's name `<project>-<service>`, also under another project name) rebuilt and rolled back, and the rollback refused when the files give the service another image or no longer define it; the compose stand-in gives the project's `name` and no `image` for such a service, as Compose does), most likely cause of a failure (log lines of several languages, health check, out of memory, `docker compose`), kept output and `log`, digests of the new build, relations in the plan, watch of dependent services, rollback (a failed one tried again, an `image:` line changed since, containers on another image), a run cut off after the restart (exception, kill) or before it (files back, entry discarded), cleanup rules, `backups`, `radar`, `retag --dry-run` (tag lines, override files, `.env` variables, anchors, refusals, nothing written), `retag --expect` (fingerprint, the written lines with owner and mode, backup, a failed `compose config` or pull restoring the files, an override file, rollback restoring them byte-exact, a file edited after the change stopping the rollback), waiting for health, `stream`, a real update process killed with SIGKILL after the restart (history, kept output, rollback; synchronised through pipes), signals (SIGTERM during the dump: exit 5, partial dump and everything else taken back; SIGHUP during the pull: exit 5; SIGTERM during `compose up`: exit 4 with history, output, rollback and the cause `signal`; SIGHUP during a rollback's restart: failed, offered again; a signal before the rollback changed a container: exit 5; the same with the real helper (`entry`), a real dump process and a real `compose up` process: state before the signal, the command gone, the lock free afterwards, the rollback), time limits (a pull or dump over its limit stops before any change, `compose up` over its limit fails with its rollback, the limits from the settings file and their ranges) |
| `test/deps_check_test.py` | `diun-deps.sh check` prints valid JSON with every field the plugin reads |
| `test/loading.js` | loading indicator, double loads, error cases |
| `test/i18n-deps.js` | DE/EN, requirements panel, install dialog |
| `test/update-backups.js` | update buttons, plan dialog, dump tick box, unknown jump size (warning, tick box), rollback offer, cause above the log, backups tab with causes (a failed rollback offered again, an aborted and a running update, the helper's own cause), output button, a build that failed before (row note, second tick box), “together with” in the rows, CI job containers left out, the warning for running CI jobs, plan groups and dependencies, delete; since 0.24.0 Rebuild (the button, the plan of `rebuild --dry-run` with the base images, no two steps and no release notes, with and without dump, the outcomes in both languages, the rollback offered after a failure, a refusal, the history note) |
| `test/diun-events.js` | which Diun event belongs to which row: its own image and tag only, none for a reference with a digest, no update for the same build under a new digest, the registry link from any event of the image, a dismissed event, the events-only view |
| `test/version-change.js` | “Change version”: rows with the button, outlined chips as buttons, the field's groups and locked entries (database, Nextcloud), preview (changed lines, plan, hints), tick boxes for a major and a floating tag, the last choice wins, refusals, `--expect` with the preview's fingerprint and `--no-dump`, failure with rollback, a stopped rollback, the history entry and its rollback dialog, no German in English; the states while the dry-run and the run work are checked at fixed points (the stub's answers wait for a gate) |
| `test/release-notes.js` | release notes in the update dialog and the preview of a version change: the range (installed, target], newest first, closed; the warning words in the summary and per release; the text as text (links, images, HTML, comments, `<script>`, angle brackets that are no HTML, nested and split markup); empty and cut texts, links only to GitHub; the slot's place; English without German, the singular; a note for every missing piece (no source, not fetched, no file, no releases, limit, error, nothing in range, the installed version unknown, nothing after it, a broken file); without a target all since the installed version; kept releases under a limit; nothing for the same version; another choice in the preview |
| `test/page-scripts.js` | not a test: the page's scripts for the jsdom tests, in the order `index.html` loads them (from `i18n.js` on, `menu-status.js` when asked for) |
| `test/partial-load.js` | a page that did not get all its scripts (each script run on its own, as in the browser): everything there (the page starts, the mark of an earlier reload gone), scripts new in a version missing (`cockpit.Packages.Reload` on the internal bus, one more load, nothing thrown, nothing run on the server), a bridge without `Reload`, still missing after that (the message in English and German, no loop), without `common.js` |
| `test/radar-cleanup.js` | “Run radar now”, cleanup box and “Clean up now”, marks, versions in the history and the update result |
| `test/version-cols.js` | columns Configured / Installed / Newest, chips, legend, not in a registry, a build after the newest release, the newest build of a registry without release tags, both build cells with stale data (marked, the error in the tooltip, no claim of a fresh check, gone after a good run), the revision of a development build, the bases of an image built here (since 0.24.0 with the version of the final base as the installed one, and Rebuild only with a newer base and for containers compose runs), a line tag refined by the radar, and kept with its patch when the registry failed |
| `test/page-frame.js` | header, tabs (mouse, keyboard, nothing loads), status labels with icons, buttons per place, dialog buttons, signature |
| `test/fonts_test.py` | every font family the stylesheets name (custom properties resolved, in the order `index.html` loads them) that has an `@font-face` has its file in `plugin/`: no request for a font outside the add-on; Red Hat Mono is the code font, from `RedHatMono.woff2`; counter-check that a font from Cockpit's files and a missing file are caught |
| `test/contrast_test.py` | WCAG contrast of every colour pair the page uses, light and dark (text 4.5:1, borders and icons 3:1) |
| `tools/check-design-files.sh` | the shared design files match `tools/design-files.sha256` |
| `test/harness.js` | prints the whole table (smoke test, also for looking at it) |
| `tools/check-version.sh` | version in `diun.js` = `?v=` in `index.html` = section in `CHANGELOG.md` |
| `tools/check-units.sh` | `systemd-analyze verify` for the units (CI lint step, next to shellcheck and ruff) |
| `test/e2e-server.sh` | real Docker host, throw-away projects `imgupd-test` (Postgres), `imgupd-test-my` (MariaDB, MySQL) and `imgupd-test-reg` (pulls against a local registry: nothing newer, registry down without and with a newer image here); since 0.19.0 every dump is restored into a fresh Postgres, MariaDB and MySQL container and compared object by object with the original (half a dump must be caught), the update lock is held by a separate process instead of a race, and CI runs it (job `e2e`, and before every release); since 0.11.0 also the tag change in the compose file, through `.env` and in an override file with their rollbacks, and the refusals for a shared variable and an anchor (the test puts a radar record for its local image into `versions.json` and removes it again); since 0.18.1 a rollback refused while the compose file names another image; since 0.20.0 `status.json` after the failed update (644, without the project folder), `status` as root and without root (`nobody`, German) with the failure, and without it after the rollback; at the end `status.json` is written again without the test project. The cleanup removes every reference of the test's images, also one by digest that a build's pull leaves without a tag, and a run that passed fails when anything of the test is left behind (containers, image references, dumps, logs, file copies, volumes). `E2E_LIB=<folder>` runs helpers that are not installed yet. Since 0.23.0 a database major (section 17, project `imgupd-test-major`): PostgreSQL 16 to 17 with test data in two databases, an extension and a role, the plan, another fingerprint refused, the data, checksums and role the same after the major, the old data kept in a volume of its own, the waiting app running again, the history and `backups`; the rollback (16 with the old data, the file byte-identical, the new major's data kept), the release after the rollback and after a second major. Since 0.25.0 the plan refused for a role with an MD5 password until its password is set again with `scram-sha-256`, the trial run before the data moves, the login over TCP after the restore, no probe container left; section 17d (project `imgupd-test-mdb`, skipped with `E2E_MYSQL=0`): MariaDB 11 to 12 (`MARIADB12_IMAGE`) with test data in two databases, a view, a procedure, an app user and a reader, the plan, the run (the trial run, `mariadb-upgrade` and the restart, the same data, both users log in over TCP, the old data in a volume of its own, the history naming the engine), the rollback (11 with the old data, the file byte-identical) and the release of MariaDB 12's data; since 0.25.1 with roles (a user with rights only through its default role, a role that has another, a role nobody has with rights of its own, a role granted with the admin option, a grant to `PUBLIC`): after the major and after the rollback the roles and every account's `SHOW GRANTS` are the same, read by the test itself, and the rights work over TCP (the default role active, the inherited and the `PUBLIC` rights, a table outside them refused, `SET ROLE` of the role with the admin option). Since 0.24.1 also majors stopped while they move the data (sections 17b and 17c, `test/e2e-pause.py` holds the installed helper after a rename in the data volume until the signal is sent): SIGTERM (it waits for the move and its record; the run fails, all old data in the volume the history names, the database stopped) and SIGKILL (the move open in the history, the data split), each followed by the rollback (PostgreSQL 16 on all of its data, the same entries, no kept volume left). Since 0.24.0 a rebuild (section 18, project `imgupd-test-build`): a Dockerfile on a base image in the local registry, the update refused, the plan with the versions of the base, a failing build changing nothing (no history, the tag and the container as they were), the rebuild bringing the newer base through `--pull` (the local tag stays on the old one), its versions, history and rollback copy, and the rollback; since 0.24.1 also a service with `build:` and no `image:` (section 18b: compose gives it no image and runs it as `imgupd-test-build-plain`), its rebuild and the rollback to its original image ID. |
| `test/upgrade_base_test.py` | `tools/upgrade-base.py`, the release the CI upgrade test starts from: a build newer than the newest release, a build of a version released already (the release before it), no older release with packages (exit 3), Semantic Versioning precedence instead of text order, drafts, pre-releases and releases without packages left out |
| `test/package-upgrade.sh` | CI (job `upgrade`), in a throw-away Debian 13 or Fedora 43 container: the release from `tools/upgrade-base.sh` (checked against `SHA256SUMS` and its build provenance), data a server has, this build over it; the version went up by the package manager's own comparison, the package database names this build, the files on disk are this build's byte for byte (`dpkg --verify` and `diff` against the `.deb`, `rpm -V` and the digests of the `.rpm`), the data unchanged, removed again with the data kept. Without an older release it says so and checks a fresh install |
| `test/cockpit-setup.sh`, `test/cockpit-session.py` | CI (job `cockpit`), a real Cockpit with systemd and Docker on a throw-away GitHub runner, Playwright with Chromium through Cockpit's login: this build installed with apt over the release before, the page reloaded and logged in again after the upgrade (the new version, `i18n.js` of this build); the page in Cockpit's menu; since 0.21.0 right after each login the page loaded hidden for the menu and not started, and the menu's status icon (the shell's `sessionStorage` and the icon) as `diun-update.py status` gives it, after the login, after the radar runs, the roll-out and the rollback, and in German for the user without sudo before the page is opened; no JavaScript error and no failed request of the add-on's files or frame, and no other kind of error than Cockpit shows on its own (a login before the add-on is installed is the baseline; on Ubuntu 24.04 Cockpit 314 answers `*/po.js` and `*/po.manifest.js` with 404 on every page; the page's own request for Red Hat Mono, which that Cockpit does not ship, was caught this way; the page of the release before is checked only for loading, its errors are listed apart in the report); with administrative access (a sudo user) and without (a user without sudo, German browser: the hint, no actions); skopeo and the radar set up from the page (timer on, a first run), “Run radar now” (a new run); a throw-away compose project (a local registry, busybox 1.36.1 running, 1.37.0 pulled under the same tag): “Roll out image” with its plan, live output and result, the container on the new image, then the rollback from the history; since 0.23.0 the Status Board's links, `cockpit.jump` from the loaded page to its places (the tab, the address, no reload) and back to the history after a tab chosen on the page. Versions, steps and results in the job summary and the artifact `cockpit-session` (with screenshots) |

The fixtures in `test/fixtures/` come from a real host. Host names are removed; `make_v2.py` builds
`inventory3.jsonl` and `versions2.json` from `inventory2.jsonl`.
