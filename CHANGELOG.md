# Changelog

## 0.9.0 – 2026-10-13

Stage 1.2, second part: the output of every update stays with the history, and a build whose update
failed is not offered silently again.

- **Output kept.** The live output of every update and rollback is stored with the history
  (`/var/backups/image-updates/logs/<op-id>.log`, root only, at most 512 KiB, without the `@@RESULT`
  line). *Show output* in the history opens it, the rollback's output after the update's. New
  read-only verb `diun-update.py log <op-id>`. An update that stops before any change keeps nothing, as
  before.
- **A build that failed before.** The history stores the registry digests of the new build
  (`new_digests`). If Diun reports exactly that build again, the row says “The update to this build
  failed on …”, and the update dialog repeats the cause and enables *Update* only after a tick box. A
  newer build or a later successful update clears the mark.
- **Restart loops:** the end-to-end test has a new step with an app that exits at once under
  `restart: always`. The update must fail within 60 seconds and name the cause with the state
  `restarting`.
- **Roadmap:** stage 2 plans a selection field for the target version (a fixed version, a version
  line such as `5`, or `latest`).
- **Tests:** offline tests for the kept output (cut at the limit, root only, nothing for an aborted
  update), the `log` verb, the digests of the new build, the output button and the failed-build hint
  with its tick box; the end-to-end test checks the kept output and the digests as well.

## 0.8.0 – 2026-10-13

Stage 1.2, first part: the update says when nobody can tell how big the jump is, and a failed update
names its most likely cause.

- **Unknown jump size.** If the installed version is unknown and the tag fixes no version number
  (`latest`, `stable` …), the update dialog says so first: the update may bring a new major version
  that needs other settings or additional services. *Update* stays disabled until a tick box confirms
  it. The plan also shows the installed version.
- **Most likely cause of a failure.** When the containers do not come up after an update or a
  rollback, a box above the log names the container and the most telling line of its log, e.g.
  “Error: Invalid environment variables: …”. For an unhealthy container it is the last output of the
  health check, for a failed `docker compose up` its own output. Without such a line the box says what
  the state tells (out of memory, exit code, not ready in time). The rollback button stays next to it.
  - The line is the last one in the log that names an error (an exception, `Error:`, `FATAL:`,
    `panic:`); stack frames never count.
  - It is part of `@@RESULT` and of the history (`reason`, `rollback_reason` for a failed rollback),
    and the backups tab shows it under the result.
- **Smaller things:**
  - The `ban` icon comes from the shared `icons.js`; the copy in `diun.js` is gone.
  - Texts and comments use plain punctuation instead of long dashes, also in the shared
    `azentrius-theme.js`. The German message for a failed update addresses the user formally.
- **Tests:** offline tests for the plan fields and for the cause (logs of Node, Python, Postgres and
  Go, health check, out of memory, `docker compose`), the dialog (warning, tick box, cause above the
  log) and the history. The end-to-end test checks the cause of its failing update and the plan fields.

## 0.7.0 – 2026-10-12

Stage 1.1: cleanup rules, radar runs on demand, versions in the history; the shared Azentrius page
frame.

- **Cleanup rules.** Old dumps and unused rollback images are deleted by rules: keep the newest 3 dumps
  per database container, remove unused rollback images after 14 days. `/etc/image-updates.conf`
  (`KEEP_DUMPS`, `ROLLBACK_IMAGE_DAYS`, `0` = never) changes them; it must belong to root, and a line
  that is not understood stops the cleanup instead of guessing.
  - Never deleted: the dumps of each project's last update, rollback images a container uses, files
    not named like the add-on's dumps.
  - Runs after every successful update (for that project and image) and, once set up, daily via
    `diun-cleanup.timer`.
  - Backups tab: the rules, the timer (with a set-up button), what would be deleted now (marked in the
    tables) and “Clean up now”. On the console: `diun-update.py cleanup [--dry-run]`.
- **Version radar after updates.** After a successful update or rollback the radar starts in the
  background, so the *Installed* column shows versions known only from labels or digests within
  minutes.
- **“Run radar now”** in the *Running containers* card: runs the radar and stays busy until the run is
  over. A second click joins the same run.
- **Versions in the history.** `operations.jsonl` records `from_version` / `to_version`; the history
  shows “16.14 → 16.15”, the update result names both. Older entries show “—”.
- **Fixed:** an update that stopped at a failing dump kept the dumps of the other databases of the
  project. It now removes them, so nothing is left behind.
- **Azentrius page frame.** The page follows Cockpit's light/dark setting and uses the shared design
  files. Nothing it does changed; helpers, formats, flows and texts stay.
  - Header in one line: “Image updates *monitor*” (the suffix in DM Serif Display italic), the state
    of the data and “Refresh” as the only filled button on the page, with its icon; while loading it
    says “Loading …”.
  - Tabs “Updates” and “Backups & dumps” with the keyboard as well (arrow keys, Home, End). Switching
    loads nothing; both tabs load together and “Refresh” keeps the tab shown. Messages and “Missing
    requirements” stand right under the tabs, in both tabs.
  - Tables in cards; on phones every row becomes a block. Status in the shared status language with
    its icon (hint, good, unknown); gold is no longer a status. The version chips keep their meaning.
  - “Update”, “Roll out” and “Roll back” are outlined buttons with an icon, “Dismiss” and “Delete” icon
    buttons. In dialogs the main action is filled and “Cancel” a link; red only confirms deleting.
  - New signature in the foot: the word mark, “Image updates monitor 0.7.0” and the server's host
    name.
  - `azentrius.css`, `azentrius-theme.js`, `icons.js`, `DMSans.woff2`, `DMSerifDisplay-Regular.woff2`
    and `OFL.txt` are byte-identical to the Cron Jobs add-on (pinned in `tools/design-files.sha256`);
    `DMSerifDisplay-Italic.woff2` holds only the letters of “monitor”. Third-party material is listed
    in `NOTICE`.
- **systemd units** are plain files in `systemd/`. `install.sh` installs them as templates to
  `/usr/local/lib/diun-radar/systemd/`, `diun-deps.sh` copies them from there and never replaces
  units already set up; `install.sh` reports when they differ.
- **Tests:** offline tests for `diun-update.py` (fake Docker, compose and systemctl), the new plugin
  parts, and `diun-deps.sh check`. The end-to-end test covers MariaDB and MySQL dumps, a wrong
  password and the new flows.
- **Tests** for the page frame (header, tabs with mouse and keyboard, labels, buttons, signature), the
  contrast of every colour pair in both themes and the shared design files. Screenshots light, dark
  and on a phone.
- **CI:** lint (shellcheck, ruff), the preview in a browser with screenshots, and
  `systemd-analyze verify` for the unit templates (`tools/check-units.sh`) run on every push and pull
  request.

## 0.6.2 – 2026-10-12

Maintenance release: the add-on is unchanged, the release is verifiable.

- **Build provenance:** the release tarball comes with a signed build provenance;
  `gh attestation verify cockpit-image-update-monitor-0.6.2.tar.gz --repo Azentrius/cockpit-image-update-monitor`
  checks that it was built by the release workflow from this repository.
- **CI:** workflow actions pinned by commit SHA and kept current by Dependabot; checkouts keep no
  token.

## 0.6.1 – 2026-10-12

Semantic Versioning, English repository, CI and releases.

- **Versions** follow Semantic Versioning. The earlier versions v1–v6 are listed below as 0.1.0–0.6.0.
  The version lives in `const VERSION` in `plugin/diun.js`; `plugin/index.html` loads `diun.js` and
  `diun.css` with `?v=<version>`.
- **Messages** of `install.sh`, the radar's log line, the tests and the preview tools are English. The
  end-to-end test runs the helpers with `DIUN_LANG=en`.
- **Docs** (README, ROADMAP, docs/INTERNALS.md) are English.
- **CI:** the `test` workflow runs the offline tests and the installer into a staging directory on
  every push and pull request; the `release` workflow makes a release on request (tarball,
  `SHA256SUMS`, notes from this file).
- `tools/check-version.sh` (part of `run-all.sh`) checks that `diun.js`, `index.html` and this file
  agree on the version.

## 0.6.0 – 2026-10-12

Columns Configured / Installed / Newest version.

- **New columns.** The table shows *Configured* (tag from compose), *Installed* (real version) and
  *Newest version*. The digest column is gone; the digest is in the tooltip.
- **Real installed version**, in this order:
  1. tag
  2. the app's version variable (`PG_VERSION`, `GLITCHTIP_VERSION` …)
  3. image label, only if that tag exists in the registry
  4. digest match via the Docker Hub API or registry manifests, with a cache
- **`16.14 → 16.15`** for images that are pulled but not rolled out yet.
- **Comparison with the installed version** in the matching scheme: `16-alpine` + 16.14 →
  `16.15-alpine`; Nextcloud `apache` → `N.N.N-apache`.
- **Chip style and legend:**
  - filled = reachable with the Update button
  - outlined = needs a tag change
  - dashed = database major
- **Status:** “unknown” instead of “up to date” when the installed version cannot be determined.
- **New:** `lib/diunlib.py` with the logic shared by inventory and radar.

## 0.5.0 – 2026-10-12

Stage 1: update, rollback, backups.

- **Buttons:** “Update” (Diun reports a new build) and “Roll out” (newer image already local).
- **Plan dialog:**
  - affected services/containers (all with the same image in the project)
  - databases with size
  - free space
  - warning when the compose file changed
- **Flow:**
  1. `pg_dumpall` or `mariadb-dump`
  2. rollback tag
  3. `compose pull`
  4. `up -d --no-deps`
  5. wait for healthy
  6. result; on failure, offer a rollback
- **Tab “Backups & dumps”:** history with rollback; delete dumps and rollback images.
- **Inventory** rewritten in Python: compose mapping, “pulled, not rolled out”, far fewer Docker calls.
- **E2E test** in a throw-away project.
- **Fixed:** helper containers using the Postgres image (`sh -c …`) were taken for databases. Now only a
  container that starts the server counts.
- **Fixed:** a plan arriving late reopened a closed dialog.

## 0.4.0 – 2026-10-12

Two languages, requirements.

- German/English following Cockpit's language (fallback English), the menu entry too.
- **Panel “Missing requirements”** checks Docker, Diun, skopeo and the radar and offers to install them
  (`diun-deps.sh`, never overwrites).

## 0.3.0 – 2026-10-12

Loading indicator.

- Loading block on the first load, spinner in the Refresh button, dimmed table.
- **Fixed:** a watch/read loop that started hundreds of inventory processes. Loads never run in
  parallel now, and a reload happens only when the file tag changed.

## 0.2.0 – 2026-10-12

Version radar.

- `diun-versions.py` + systemd timer (every 6 h): newer version tags (patch/minor/major) via
  `skopeo list-tags`; database majors as grey info.
- **Security:** everything that runs as root moved to `/usr/local/lib/diun-radar`, away from
  `/var/lib/diun`, which the Diun container can write to.

## 0.1.0

First version.

- Table of running containers with Diun reports (new digest for the same tag), “Dismiss” button.
