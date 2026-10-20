# Changelog

## 0.23.0 – 2026-10-20

A click on a tile of the Status Board opens the page where the finding is: the tab, and there the
first row of that kind. The menu icon warns as well when the radar reached no registry. An update can
now be done in two steps: download now, roll out later. The update and version change dialogs show
the release notes between the two versions, from GitHub (stage 3).

**Upgrading:** nothing to do. The next radar run or update writes `status.json`; `status` gives the
links at once. The release notes come with the next radar run (the package starts one two minutes
after the install, when the radar is set up).

- **Links for the Status Board.** Every item of `diun-update.py status` now carries a `link` to its
  place on the page: “Updates available” `#/updates/update`, “Roll-out pending” `#/updates/rollout`,
  “New versions” `#/updates/new-versions` (the Updates tab, scrolled to the first row with that
  status), “Failed updates” `#/backups/history` (the update history on the Backups tab) and the
  radar `#/updates`. The tile of the Status Board goes to the link of its most important item.
- **The page follows its location.** Cockpit keeps a page that is loaded and, when the Status Board
  jumps to it again, only changes its location. The page now listens to `locationchanged`: every new
  location shows its tab at once and scrolls to its row once the rows are there and the page is
  shown (also when Cockpit loaded it hidden after the login). A tab chosen on the page goes into the
  address too, so the link to the place shown before leads there again. Without a place the page
  shows the Updates tab as before; a path that is no place changes nothing.
- **Fix: the menu icon and “no registry reached”.** 0.22.0 gave the Status Board the warning
  “Version radar reached no registry”, but the icon in Cockpit's menu did not read the mark in
  `status.json` and showed no warning for it. It now does, with the same text as the status.
- **Tests.** `test/places.js` (new): every place opened directly and while the page stays loaded,
  a tab chosen on the page and the link back, a reload that scrolls nowhere, no place, unknown paths,
  no row of a status, the page loaded hidden (the location before and after the shell's hint), and
  every link of `test/fixtures/status.json` against the Status Board's rule and the rows its item
  counts. `test/status_test.py`: the link of every item in every state and language, and a link the
  board would not take. `test/menu-status.js`: a radar run that reached no registry, also outdated
  and next to a failed update, against `diunstatus.py`. The Cockpit job in CI jumps with
  `cockpit.jump` into the loaded page (the tab, no reload) and back after a tab was chosen on it.
- **Update in two steps** (the last open item of stage 1.2). The update dialog offers, below the plan
  and its warnings, “Update now” or “Download only” (“Nur herunterladen”). Download only runs the new
  verb `diun-update.py pull <container>`: the update lock, the same plan and refusals as `update`, then
  `docker compose pull` of the plan's services under `DOCKER_TIMEOUT`, and nothing that runs changes
  (no dump, no rollback copy, no history entry). The row then shows “Roll-out pending”, and “Roll out
  image” later runs the update as before, e.g. in the maintenance window. A failed pull, a time limit or
  a signal is exit 5; nothing newer is `nochange`; `status.json` is written again. Tests:
  `test/update_test.py` (pulled with the version that came, only the plan's services pulled and nothing
  else done, the tag on the new image while the containers keep the old one, nothing newer, a failed
  pull, a time limit, a signal, a wrong call), `test/update-backups.js` (the choice in German and English,
  the button's label, the dump moot, the command, the result; no choice for an image already here) and
  a new section 10d in `test/e2e-server.sh` (pull only against the local registry: the tag moves, the
  container and the history stay; then the roll-out).
- **Release notes in the dialogs** (stage 3). The radar names each image's repository on GitHub
  (`source` in `versions.json`: a short list of checked images such as `nextcloud` → nextcloud/server
  and `redis` → redis/redis, the label `org.opencontainers.image.source` or `org.label-schema.vcs-url`,
  or `ghcr.io/<owner>/<name>`) and, for the images with something newer, fetches their releases into
  `/var/lib/diun-radar/releases.json` (new module `lib/diunnotes.py`). The update dialog and the
  preview of a version change show, closed below the plan, the releases newer than the installed
  version and up to the target (all since the installed version for a tag like `latest` that names
  no target), newest first, each with its date, its text and a link to GitHub; a
  warning label names *breaking*, *migration* or *deprecated* when a text mentions them, and the text
  marks them. The text is shown as text: Markdown's headings, lists and links read as such, images,
  HTML and comments are left out. When there are none, a note says why (no source, not fetched yet,
  no releases, GitHub's limit, an error, none between the two versions). GitHub without a login allows
  60 requests per hour: one request per image with something newer, at most 20 per run, none with 5
  or fewer left, an answer kept for 24 hours (sooner when a newer version turns up, never within an
  hour); on the limit or without network the last answer stays. A problem there never fails the radar
  run. Tests: `test/notes_test.py` (new: the sources, the releases of an answer and which of them, when
  a source is asked again, the limit, errors, the radar run writing both files), `test/release-notes.js`
  (new: the dialogs, the text as text, every note, English and German), the Cockpit job in CI checks
  that the hardened radar unit writes `releases.json`.
- **Tests without a tag filter.** Five jsdom tests took the `<script>` tags out of `index.html` with a
  regular expression, which CodeQL reports as an incomplete tag filter (`js/bad-tag-filter`). They
  now remove the script elements from the parsed page, as the other tests do.
- **The page's code in eight scripts** (technical debt in the roadmap). `plugin/diun.js`, about 2,000
  lines, is split into `common.js`, `dialog.js`, `notes.js`, `update.js`, `retag.js`, `overview.js`,
  `backups.js` and `diun.js`: classic scripts that share `window.ImageUpdatesPage`, loaded by
  `index.html` in that order (no ES modules: jsdom, which runs the tests of the page, runs none). The
  code moved as it was; only the calls between the scripts changed, and the counter that drops an
  answer arriving after another dialog opened is `ticket()` in `dialog.js`. The jsdom tests take the
  scripts from `index.html` (`test/page-scripts.js`), and `tools/check-version.sh` checks their `?v=`.
  Cockpit's bridge serves a file new in an update only after it read the packages again, which the
  shell asks for while it loads the page: after an update and a reload of the browser a script can be
  missing (seen in CI with Cockpit 314). The page then asks the bridge to read the packages again
  (`cockpit.Packages.Reload`) and loads once more; if a part is still missing, it says to log out and
  in again. Tests: `test/partial-load.js` (new); the Cockpit job accepts a 404 for a file new in this
  build in the reload right after the upgrade only when the page then shows the new version.

## 0.22.0 – 2026-10-20

A radar run that reaches no registry at all tries once more and then says so, an install no longer
starts the radar in the middle of the `apt` run, a rollback copy that is gone is named, and an image
with nothing new for over a year is pointed out.

**Upgrading:** nothing to do. This upgrade itself already starts the radar two minutes later.

- **No registry reached.** On build-host (2026-10-20) the resolver answered “server misbehaving” for
  every name during two radar runs: all 21 images got a registry error, and the page showed “(older
  data)” 20 times without saying why. When every image of a run ends in a registry error (an
  “unlisted” image counts as an answer), the radar now waits 30 seconds and tries all of them once
  more. If no registry answers then either, `versions.json` carries `no_registry` with the most
  frequent error, the header says “(no registry reached)” with the error and a pointer to DNS and
  the network in its tooltip, and the Status Board and the menu icon show the warning “Version radar
  reached no registry” (without the error, which can name addresses). The entries stay the last good
  ones, marked “(older data)” as before.
- **The radar after an install.** The package started the radar at once after every install or
  upgrade, twice even: `configure` restarted the running radar timer, and a timer whose `OnBootSec=`
  lies in the past starts its service at once, then it started the service itself. Both fell into
  the same `apt` run, which can restart `systemd-resolved` or the network. Now a running timer is
  left alone (`daemon-reload` reads the package's unit), and the refresh runs two minutes later by a
  timer `diun-radar-after-install` (at once only where `systemd-run` fails). Removing the package
  stops that timer.
- **Tests.** `test/radar_test.py`: a run that reaches no registry and the second try that works, one
  that fails twice, one registry down, no images, every registry answering “denied”, and `main()`
  writing `no_registry` next to the kept entries. `test/status_test.py`: the warning, its summary and
  detail in English and German, no address in it, an old run outdated first. `test/version-cols.js`:
  the header and its tooltip. The Cockpit job in CI runs the package's `configure` again on the real
  runner: the timer `diun-radar-after-install` waits, the radar does not start, its timer keeps running.
- **Rollback copy gone.** A `docker image prune -a` of another tool (Docker Manager, a cleanup script)
  deletes the rollback images, and the history then only left the rollback button out without saying
  why. `backups` now marks such a run `rollback_gone`, and the history says “Rollback copy gone” with
  the image and the possible causes in the tooltip. Tests: `test/update_test.py` (a run whose copy is
  gone, a failed rollback whose copy is gone, no mark where the copy is there, was rolled back or never
  existed) and `test/update-backups.js` (the note in German and English, no rollback button).
- **Nothing new for over a year.** “Up to date” said nothing about age: `ronmi/forgejo-mcp` runs the
  newest `latest`, built on 28 Oct 2025 and unchanged since. When the container runs the newest there is
  and its image was built over a year ago, the newest column adds “nothing new for over a year”; the
  tooltip names the build date and says the project may be abandoned. Not where something newer is known
  (also a database major) and not for a fixed date before 1980; the status stays up to date. Tests:
  `test/version-cols.js` (a version and the newest build of a registry without release tags, built over
  a year ago, in German and English; none for a recent build or a database major).

## 0.21.2 – 2026-10-20

A failed update stays failed until a later run of the same services settles it, and the newest
build of `latest` says when its data is older because the registry did not answer.

**Upgrading:** nothing to do. The next radar run or update writes `status.json` with the new rule.

- **Fix: a success of another service hid a failure.** The status for the Status Board and the menu
  icon took the newest run of each repository in a compose project: when `db16` (`postgres:16`) failed
  and then `db17` (`postgres:17`) in the same project was updated, the failure was gone although
  `db16` was still affected. A run now stands for what the update grouped: its services in its
  compose project and compose files, and the containers it names. A later run settles only those, in
  the same files; a retry or a tag change of the same services still settles a failure. A run partly
  settled stays, named by a container still affected and with the services still affected (“shop-db-1
  (still affected: db)”). History entries without services (written by old versions) stand for the
  containers they name and settle no services, so their failure stays until a later run of those
  containers or a rollback.
- **Fix: “newest build” over stale data.** When the radar could not reach the registry it keeps the
  digest `latest` had in the run before and marks the entry stale, but “newest build”, “other build”
  and “build after vX” left the mark out and said there was nothing newer. They now carry “(older
  data)” like every stale entry, with the registry's error and the time of the last good run in its
  tooltip, and say only what that run found. The mark's tooltip on the other columns names the error
  and the time as well.
- **Tests.** `test/status_test.py`: services and tags of one project, the same image in separate
  compose files and in another directory, a retry and a tag change, overlapping groups, rollbacks and
  aborted runs next to an unrelated success, old entries, and the state, summary and detail they give.
  `test/radar_test.py`: an image without release tags through `keep_last_good` (good, registry down,
  back). `test/version-cols.js`: both build cells with stale data in German and English, another build,
  and the mark gone after a good run.

## 0.21.1 – 2026-10-20

An own image that the registry has no release tag for (`latest` built from the main branch, e.g. on
ghcr.io) is up to date when the container runs the build the tag points to, instead of unknown.

**Upgrading:** nothing to do.

- **Fix: “Unknown” for the newest build of `latest`.** The radar looks up the digest a tag like
  `latest` has in the registry also outside Docker Hub, but the overview counted the container as up
  to date only when a version tag had the same digest or, on Docker Hub, when the build was pushed
  after the newest release. An image whose registry has no release tag at all (Sharewerk on ghcr.io,
  built from its main branch) therefore stayed “Unknown” with an empty newest column, and the
  tooltip wrongly said the radar had not covered it yet. Now the newest column says “newest build
  from <date>” with a check mark and the status is “Up to date” when the container runs the build the
  tag points to, and “other build” (status unknown) when it runs another one. The date is when the
  build was made: an abandoned image (`ronmi/forgejo-mcp`, whose `latest` is from 28 Oct 2025) is the
  newest build there is, but its age stays in sight. With release tags in the registry
  the column keeps “newest: X”: outside Docker Hub nothing tells whether the build is newer than the
  newest release.
- **Tests.** `test/version-cols.js` checks both cases in German and English, the date, no date for a
  reproducible build (1970), and that a registry with release tags keeps “newest: X” and the unknown
  status.

## 0.21.0 – 2026-10-19

Cockpit's menu shows how the add-on stands: an icon next to “Image Update Monitor” with the summary
of its status as the tooltip.

This release also brings 0.20.0 and 0.20.1, which were not released on their own: the status for the
Azentrius Status Board (`diun-update.py status --lang en|de`, also without root) and the removal of the
helpers' `__pycache__` together with the package; their sections in
[CHANGELOG.md](https://github.com/Azentrius/cockpit-image-update-monitor/blob/main/CHANGELOG.md) tell more.

**Upgrading:** nothing to do. The icon appears after the next login, once the radar has run.

- **Status icon in the menu.** The same state as for the Status Board, with Cockpit's own icons: red
  for a failed update, yellow for an outdated version radar, blue for updates available, roll-outs
  pending or new versions; no icon when nothing needs doing. The tooltip is the summary in English or
  German (“2 updates available, 3 roll-outs pending”). It follows `/var/lib/diun-radar/status.json`, so
  it changes with every radar run, update, rollback and cleanup, and turns yellow when the radar has
  not run for 24 hours. It works without administrative access.
- **Loaded hidden after the login.** A page can set only its own menu entry's icon, so the manifest
  asks Cockpit to load the page right after the login (`preload`, as Cockpit's own Software updates
  page does). Until it is opened, the page reads only `status.json` and runs no helper; the new
  `menu-status.js` sets the icon, and `diun.js` starts when the page is shown.
- **Tests.** `test/menu-status.js` checks the icon, the tooltip in both languages against the
  summary of `diun-update.py status`, and that nothing starts while the page is hidden; the Cockpit
  session in CI checks the same in a real Cockpit after each login, after the radar runs, the roll-out
  and the rollback, and for a user without administrative access.

## 0.20.1 – 2026-10-19

Removing the package now removes the helpers' folder as well, also after the helpers ran as root.

**Upgrading:** nothing to do. A folder `/usr/lib/cockpit-image-updates` that an earlier removal left
behind holds only `__pycache__`; `sudo rm -r /usr/lib/cockpit-image-updates` removes it.

- **Fix: `/usr/lib/cockpit-image-updates` stayed after `apt remove`, `dnf remove` or `zypper remove`.**
  The helpers run as root (the page, the radar, the cleanup), and Python writes the compiled
  `diunlib`, `diunproc` and `diunstatus` to `__pycache__` next to them. No package owns these files,
  so the package manager left them and the folder in place. The package now removes only that
  `__pycache__` before it is removed (`packaging/scripts/remove`, also `make uninstall`); data, dumps,
  rollback images and the Diun stack stay as before.
- **Tests.** The install job asks for the status as root without `-B`, as a server runs the helpers,
  checks that `__pycache__` is there and that the folder is gone after the removal; the upgrade test
  (`test/package-upgrade.sh`) does the same on Debian 13 and Fedora 43 after the upgrade.

## 0.20.0 – 2026-10-19

The add-on tells the Azentrius Status Board how it stands: `diun-update.py status` gives the shared
status of the Azentrius add-ons, also without administrative access on the host.

**Upgrading:** nothing to do. The status appears with the next radar run; the package starts one after
the install when the radar is set up.

- **Status for the Status Board.** The new read-only verb `status --lang en|de` answers with the status
  contract of the Azentrius add-ons (schema 1): updates available, failed updates, an outdated version
  radar, roll-outs pending and new versions, each with the names of the containers, its state and when
  it was checked, and a summary in English or German. `plugin/manifest.json` names the command in a
  new section `azentrius`, where the Status Board finds it.
- **Without root.** `status` reads `/var/lib/diun-radar/status.json` (0644, with the names of containers,
  states and times only). The radar run writes it next to `versions.json`, and every update, tag change,
  rollback and cleanup writes it again, silently. As root `status` also reads the history again; the
  radar's age is judged when it is asked. Without the file the state is `none`, with the reason.
- **The overview's rules.** It counts the rows the overview marks “Update available”, “Roll-out pending”
  and “New version”, and a failed update until a later one of the same image succeeds or it is rolled
  back. A new test checks with the data of a real host that the page shows the same rows.
- **Tests.** `test/status_test.py` and `test/status-rows.js`; the install job asks the installed package
  for its status as root and as `nobody`; the end-to-end test checks the file and the verb after a
  failed update and after its rollback.

## 0.19.1 – 2026-10-18

Azentrius moves to azentrius.com; the page and the helpers work as before.

- **Signature:** the word mark links to azentrius.com.
- **Package repository:** README, SECURITY and release notes name packages.azentrius.com, the new
  address of the Azentrius package repository; packages.azentrius.de keeps serving the same packages.
- **Contact:** security reports without GitHub go to support@azentrius.com, which is also the
  maintainer of the `.deb` and `.rpm`.

## 0.19.0 – 2026-10-18

Ready for wider use: docker commands and dumps have time limits and a stopped run cleans up before it
frees the lock, every dump is restored and checked in CI against a real Docker Engine, the page is
tested in a real Cockpit, the supported systems are named and the packages enforce the minimum
versions, and a killed update keeps its output. Line tags show the release they run.

**Upgrading:** install the package as usual (`apt` or `dnf` from packages.azentrius.de, or the checked
release files, on openSUSE with `zypper`); nothing to change by hand.
New, optional: `DOCKER_TIMEOUT` and `DUMP_TIMEOUT` in `/etc/image-updates.conf` (defaults 30 minutes
and 4 hours); raise `DUMP_TIMEOUT` for very large databases. Cockpit now checks its version (264 or
newer), and Docker Compose older than 2.8 counts as missing. The packages now depend on
`cockpit-bridge` 264 or newer and `python3` 3.8 or newer: on older systems (Ubuntu 20.04, RHEL 8 and
its derivatives, openSUSE Leap 15.6) the package manager refuses the update.

- **End-to-end test in CI, dumps restored.** `test/e2e-server.sh` runs on every push against the
  Docker Engine of a throw-away GitHub runner, and before every release. It restores every dump the
  helper writes into a fresh Postgres, MariaDB and MySQL container and compares rows, views,
  functions, procedures, triggers, events, indexes, sequences, users and grants with the original;
  half a dump must be caught. Pulls run against a local registry: nothing newer, registry down with
  and without a newer image here. The cases that still expected “no change” for a tag that cannot be
  pulled now expect the stop before any change (exit 5).
- **The page in a real Cockpit, in CI.** The new job `cockpit` installs Cockpit on a throw-away GitHub
  runner (Ubuntu 24.04, with systemd and Docker) and drives the page through Cockpit's login in
  Chromium: this build installed with apt over the release before, the page reloaded and logged in
  again after that upgrade, found in the menu, without JavaScript or resource errors of its own (and
  none in Cockpit beyond those it shows without the add-on), with and without administrative access; skopeo and the radar set up from the page and a radar run; on a
  throw-away compose project an update with its plan, live output and result, and its rollback.
  The job summary lists the versions and every step, the artifact `cockpit-session` has screenshots.
- **More checks in CI:** the helper tests on Python 3.8; the packages installed and removed on
  Debian 12, Ubuntu 22.04 and 24.04, Rocky Linux 9 and 10 and openSUSE Leap 16.0, skopeo installed
  there the way the page does it; gitleaks over the whole history.
- **Upgrade test that holds after a release.** The job `upgrade` took the newest release as the base
  and required another version, so after 0.19.0 every pull request without a new version would have
  failed. It now starts from the newest release older than the build by Semantic Versioning (not a
  draft or pre-release, with packages; `tools/upgrade-base.py`), checked against `SHA256SUMS` and its
  build provenance, and checks on Debian 13 and Fedora 43 that the version went up by the package
  manager's own comparison, that the installed files are this build's byte for byte and that the
  data stays over the upgrade and the removal. Without an older release it says so and checks a
  fresh install.
- **Packages require the minimum versions.** `cockpit-bridge (>= 264)` and `python3 (>= 3.8)` in the
  `.deb`, `cockpit-bridge >= 264` and `python3 >= 3.8` in the `.rpm`, instead of any version. CI checks
  the metadata and that Ubuntu 20.04 (Cockpit 215) and Rocky Linux 8 (`python3` 3.6) refuse the
  package for that reason.
- **Releases wait for every test job.** The release workflow checks that the whole test workflow
  passed for the commit it releases (the end-to-end test, the installs, the upgrade, the Cockpit
  session), not only the required check `test`.
- **Security policy and contributing:** `SECURITY.md` says which releases get fixes (the newest) and
  how to upgrade and report; `CONTRIBUTING.md`, issue forms (with a warning against pasting
  credentials) and a pull request template.
- **UI tests without fixed waits.** The plugin tests waited 5 to 400 ms for the stubbed helpers, which
  a slow CI runner could miss. They now wait until every answer has arrived and the page has handled
  it; states in between (a slow inventory, a running preview) are checked behind answers the test
  releases itself.
- **Maintenance: the page's texts in a file of their own.** The English and German texts (`I18N`)
  moved unchanged from `plugin/diun.js` to `plugin/i18n.js`, which `index.html` loads first; `diun.js`
  keeps the code (1755 instead of 2445 lines), and a translation can be reviewed on its own.
- **Supported systems defined.** The README names what CI tests (every helper against the real Docker
  Engine of a GitHub runner, Python 3.8 and 3.12), where the packages install (Debian 12/13, Ubuntu
  22.04/24.04, Fedora 43, Rocky Linux 9/10, openSUSE Leap 16.0) and what is expected but untested.
  Minimums: Cockpit 264, Docker Compose 2.8 (`up --pull never`; 5.x included), Python 3.8.
- **Fix: Cockpit never checked the minimum version.** `plugin/manifest.json` said `require`, a key
  Cockpit does not read; it is now `requires` (Cockpit 264, Ubuntu 22.04's).
- **Compose older than 2.8 counts as missing.** The requirements check and the Diun setup now say
  "Docker Compose 2.8 or newer" instead of failing later in an update.
- **openSUSE:** skopeo installs with zypper as well. README, `SECURITY.md` and the release notes
  give one way for openSUSE: the release's `.rpm`, checked against `SHA256SUMS` and its build
  provenance first, then `zypper install --allow-unsigned-rpm`; the package repository has no openSUSE
  packages. Leap 15.6 (Python 3.6) is not supported.
- **Time limits for docker commands and dumps.** A `docker compose pull`, `docker compose up`,
  `docker rmi` or database dump that hung held the update lock for good: no other update, rollback or
  cleanup could run. Each now has a limit, `DOCKER_TIMEOUT` (default 1800 s) and `DUMP_TIMEOUT`
  (default 4 h) in `/etc/image-updates.conf`, checked like the cleanup rules. The limit counts for the
  whole command, also when it prints nothing; then its process group is stopped (SIGTERM, SIGKILL after
  10 s) and the lock is free again. A pull or dump over its limit stops the update before anything
  changed and removes the partial dump; a `compose up` over its limit fails the update with its history,
  output and rollback, and says that the containers may still be changing. The process handling lives
  in the new `lib/diunproc.py`.
- **Fix: SIGTERM or SIGHUP left the docker command running and the lock free.** The helper ended at
  once; a `docker compose up` or dump it had started went on, and the next update or rollback could
  start meanwhile. Now the first SIGTERM, SIGHUP or Ctrl-C ends the run through its cleanup: the
  command's process group is stopped, a run that changed no container yet takes back its dumps,
  rollback copy and files (exit 5), one in the restart fails with its history, output and rollback
  (exit 4, cause `signal`), and the lock stays held until all that is done; a second signal cannot
  cut it short. SIGKILL cannot be caught by any program: after it, the docker command keeps the lock
  until it has ended, and the run stays in the history as aborted with its rollback.
- **Fix: a line tag showed its own number as the installed version.** A tag such as `cubejs/cube:v1.6`,
  `onlyoffice/documentserver:10.0`, `clickhouse/clickhouse-server:24.12-alpine` or
  `pomerium/pomerium:v0.33` names a line, not a release: the registry has `v1.6.74`, `10.0.0.1`,
  `24.12.6.70-alpine` and `v0.33.4` under it. The add-on took the tag's number as the exact version, so
  the installed column showed “1.6” and the history “1.6.6 → 1.6”. The radar now recognises a line tag
  by those longer versions and names the release by digest (1.6.74), compares it in that scheme and
  shows what the line points to now (“v1.6 = 1.6.75”). The page, the update plan and the history take
  that version over the tag's number when it refines it. Exact tags (`16.14-alpine`, `v2.1.4`) stay
  as they are, and a version variable in the image still wins. When the registry fails, the same image
  keeps the release and the patch it had, marked as older data.
- **Fix: a font request that failed on Ubuntu.** The shared stylesheet asked Cockpit for Red Hat
  Mono (`static/fonts/RedHatMono`), which Ubuntu 24.04's Cockpit 314 does not ship: HTTP 404 on every
  load of the page, found by the Cockpit session, and code was shown in a fallback (Courier New or the
  browser's monospace font). The page now brings Red Hat Mono itself, as the design system asks:
  `RedHatMono.woff2` (a Latin subset, SIL Open Font License 1.1, in `OFL.txt` and NOTICE) comes with
  the new shared design files from Cron Jobs (`azentrius.css`, `OFL.txt`). `test/fonts_test.py`
  checks that every font the page uses ships with the add-on.
- **Fix: a killed update kept no output.** The output of an update was written to its log file only
  at the end. A run killed after the restart (SIGKILL) stayed in the history with its rollback, but
  without its output. It now goes to the file as it is printed once the run is in the history, so it is
  kept up to the kill; after a power cut the last lines can be missing.

## 0.18.1 – 2026-10-18

Updates stay recoverable: one cut off after the restart keeps its history and rollback, a rollback
checks what compose would start and what runs afterwards and can be tried again after a failure, and a
registry error no longer looks like “no newer image”. The history fills versions the radar finds
later, and an image built on Eclipse Temurin says which Java version it carries.

- **Fix: an update cut off after the restart kept no history.** The operation was written only after
  the containers were recreated and checked. If the helper stopped in between (Docker not answering,
  the process killed), the new image ran without a history entry, kept output or a rollback from the
  page. The run is now in the history before the first change a rollback has to undo: before a tag
  change writes its files and before the containers are recreated. An error after the restart fails
  the update with its cause; a run that ends without its last line stays “aborted” with its rollback.
  A run that stops before any container changed still takes back what it did and leaves no entry.
- **Fix: a rollback could report success on the wrong image.** It only pointed the old tag back to the
  rollback copy and started what the compose files name now. With an `image:` line edited after the
  update (`latest` → `2`), compose started the other image and the rollback said “successful”. The
  rollback now stops before any change when the files give the services another image, and afterwards
  checks that the containers run the rollback copy.
- **Fix: a failed rollback could not be tried again.** It was marked “rolled back” as well, and a
  second try was refused while the new image still ran. A failed rollback is now offered again; a tag
  change's files that are back already stay as they are, and the outputs of both tries are kept. A
  rollback that went through is still not repeated.
- **Fix: a registry error looked like “no newer image”.** The pull ran with `--ignore-pull-failures`,
  so compose exited 0 when the registry failed, and with the old image still there the update ended as
  “no change”. The pull now reports the error: the update stops before any change and names it, unless
  the newer image is on the server already, which is then rolled out with a warning.
- **History: versions the radar finds later.** An update of an image whose version only the radar
  can tell (`ollama:latest`, an image with a version label) recorded no new version, because the
  radar names a new image a few minutes after the update. The history kept showing “0.30.11 → ?” or no
  versions at all. The backups tab now fills a missing version from the radar's last run for that
  image ID; the history file itself stays as written.
- **Fix: Java version of an Eclipse Temurin base.** For an image built on the server from
  `eclipse-temurin:21-jre-jammy` the radar could only tell that the base was published after the
  build, not which Java version the build carries: Temurin names its release
  `JAVA_VERSION=jdk-21.0.12.1+1`, which the helpers did not read. The `jdk-` is now skipped, so the
  base line says what the build carries (21.0.12.1, as the tag `21.0.12.1_1-jre-jammy` reads), and a
  build on an older Java counts as behind its base even when it was made after the push from a cached
  base (without `--pull`). A container of `eclipse-temurin` itself gets its version from the variable
  too. Found on a host with a Java app built on Temurin.

## 0.18.0 – 2026-10-18

Images without a registry version say more: a development build shows its commit, and an image built
on the server is checked against the images its Dockerfile starts from. MariaDB's version variable is
read again.

- **Fix: MariaDB version from its variable.** The official image names its version with the Debian
  epoch (`MARIADB_VERSION=1:11.8.9+maria~ubu2404`), which the helpers did not read: the version came
  only from the radar, and the history of an update to 11.8.9 showed no new version because the
  radar did not know the new image yet. The epoch is now skipped.
- **Development builds show their commit:** an image without a version that names its source revision
  (label `org.opencontainers.image.revision`), such as `pomerium:main`, shows “main @35b2a97” in the
  installed column instead of “?”, with the full revision and the build date in the tooltip. The
  inventory names it (`revision`).
- **Images built on the server:** an image built by compose (`build:`) is in no registry and showed
  “not in a registry”. The radar now reads its Dockerfile's `FROM` lines and checks those images: what
  their tag points to and when the registry got it for this server's platform (`built` and `base` in
  `versions.json`). A base that is newer than the build shows as “base newer: caddy:2 = 2.11.7”, a new
  version, with the rebuild command in the tooltip; otherwise “base current”. Found on a test host,
  where Caddy with a DNS plugin was built in June on Caddy 2.11.4 while caddy:2 had reached 2.11.7.

## 0.17.0 – 2026-10-18

A new digest of the build that already runs, as when a registry publishes an image's list of platforms
again, no longer shows as “Update available”.

- **Fix: “Update available” for the build that runs.** Docker Hub publishes the list of platforms of an
  official image again when it adds one (`nextcloud:apache` got riscv64 on 2026-10-16). The list's
  digest changes, the amd64 build stays, and a row compared only digests: a container updated a day
  earlier showed an update again, which would have restarted it with the same image. A new digest
  now counts only when Diun's build time (`created`) differs from the running image's, which the
  inventory now names (`created`). A fixed build date before 1980 (reproducible builds) counts for
  nothing, so such images still compare by digest.

## 0.16.0 – 2026-10-17

Containers of running CI jobs are counted above the table instead of listed, and updating a CI runner
while jobs run asks first, because those jobs break off.

- **CI jobs:** the containers of running jobs of a Forgejo runner or Gitea's act_runner
  (`FORGEJO-ACTIONS-TASK-2415_WORKFLOW-…_JOB-unit`) were listed as rows with “not in a registry” and
  “Unknown”; they live as long as the job and cannot be updated. They are no longer listed: the line
  above the table counts them and its tooltip names them. The inventory marks them (`ci_job`).
- **Warning before updating a runner while jobs run:** the plan of a runner image lists the running
  CI jobs (`ci_jobs`), and the update waits for a tick box. On a CI host two jobs kept running without
  a runner after the runners were updated. CI job containers are also left out of the shared network
  hint.

## 0.15.0 – 2026-10-17

An update takes only the services started from the same compose files, so a project split over several
files can be updated, and a pull that fails is reported as such instead of “no newer image”.

- **Fix: update of a project split over compose files.** On a CI host one compose project ran from
  two files started one by one: `docker-compose.yml` with three runners, `docker-compose.emu.yml`
  with a fourth, all on `forgejo/runner:12`. The update took all four, but called compose with the
  file of the container clicked; `compose pull` stopped at once with `no such service: runner-emu`,
  and the add-on reported “no newer image”. The runners could never be updated. Now an update takes
  the services of the same image **and the same compose files**; the others show “Roll-out pending”
  afterwards and are rolled out from their own file. “Together with …” in the overview counts the
  same way (the inventory names each container's compose files, `files`).
- **A failed pull is no “no newer image”:** when `compose pull` fails and the old image stays, the
  update stops (exit 5) with “the pull failed – nothing changed”; rollback tag and dumps are removed
  again, as with any stop before a change.
- **Plan:** a service the compose files do not define (any more) stops the plan with the reason
  `service-missing` instead of failing in the middle of the update.

## 0.14.0 – 2026-10-16

“Update available” only from Diun's reports for the row's own tag, and a `latest` that points to no
release but to a newer build says so: “build after v4.5.3”, up to date when exactly that build runs.

- **Fix: “Update available” from another tag.** The page matched Diun's events by image only, so the
  newest event of an image counted for every tag of it: an event for `postgres@sha256:…` (a CI job's
  service container) marked every PostgreSQL row on a test host right after their updates, and
  `nextcloud:fpm` would have marked `nextcloud:apache`. A row now takes only the event of its own
  image and tag. An event for a reference with a digest belongs to no row: a pinned image does not
  follow its tag, and the Update button could not bring that build anyway (the plan refused with
  “pinned by digest”).
- **Build after a release:** a `latest` (or `main`) that points to no release tag showed “?”,
  “newest: v4.5.3” and “Unknown”, although it was newer than that release: MobSF builds `latest` from
  its development branch, 4.5.4 while v4.5.3 is the newest release. On Docker Hub the radar now notes
  when the tag was pushed after the newest release (at least a day later; `tag_now.pushed` and
  `tag_now.after` in `versions.json`), and the row says “build after v4.5.3”. When the container runs
  exactly that build, the row is up to date. Other registries do not say when a tag was pushed.

## 0.13.0 – 2026-10-16

Image Update Monitor: a new name and the package `cockpit-image-update-monitor`, which replaces
`cockpit-image-updates`. The page follows the Azentrius design system where it did not yet: status
never in colour alone and never in gold, alerts instead of stripes at the edge, empty states that say
why, targets large enough for a phone, and clearer texts. A database started with options only now gets
its dump before an update too.

- **Fix: database dump skipped** for a MariaDB, MySQL or PostgreSQL container whose compose file
  gives the server options only, as Nextcloud's example does (`command: --transaction-isolation=…
  --log-bin=binlog`, or `-c max_connections=200`). The image's `docker-entrypoint.sh` starts the server
  with them, but the add-on looked for the server's name in the command and did not count the
  container as a database: the plan showed no dump, and an update ran without one. Found before a
  Nextcloud major update on a test host. The end-to-end test's MariaDB now runs with such options.
- **Newest version:** each level (patch, minor, major) is a label with the level as a word. A level the
  Update button brings is a hint (`info-circle`), one that needs a different tag is a label without
  judgement, a database major one with `ban`. The green, gold and red chips are gone; how a level is
  reached is in its tooltip and in the legend. “Installed is the newest” shows `check-circle` instead
  of “✓”.
- **Alerts:** the warnings in the plan of an update or a version change (unknown jump, new major
  version, a build that failed before, compose file changed, start order, not possible) are warning
  alerts with the triangle and a title; the notes (shared network, tag from a variable, git repository,
  what a rollback restores) are info alerts; a check that fails is an error alert. Messages for the
  whole page get a title, and errors are announced at once (`role="alert"`). Backups that cannot be
  loaded show an error alert above the cards instead of red text.
- **Errors never in colour alone:** a database that does not run, cleanup rules that are not
  understood and the outcome line of a dialog carry their icon. Docker missing or unreachable is an
  error (`az-label--danger` with `exclamation-circle`), and so is the requirements panel then.
- **Empty states:** the containers table and the history, dumps and rollback images cards say what is
  empty and why, with an icon.
- **Targets:** level labels that open “Change version” and the tick boxes in the dialogs are at least
  29 px high, 44 px on a phone.
- **Texts:** buttons name what they act on (“Roll out image”, “Install skopeo”, “Set up radar”,
  “Roll back update”, “Clean up dumps and images”, “Delete dump”); error messages say what happened and
  how to fix it; the German texts address the user as “Sie” throughout; paths and commands are set as
  code.
- **Sizes** are on a 1024 base with two decimals (“1,67 GiB”). The helper reports the size of rollback
  images in bytes and their creation time in ISO 8601 (`backups`), and the page formats both like
  everything else; Docker's text from an older helper is still shown.
- **Signature:** the word mark links to azentrius.de; a link to the source code follows the host.
- **Motion:** the table dims within 120 ms; with reduced motion the spinners stand still. The loading
  spinner is grey instead of gold.
- **Licenses:** NOTICE names the icons as Font Awesome Free 5.15.4 by Fonticons, Inc. via
  `@patternfly/react-icons` 6.4.0. The CC BY 4.0 notice for this add-on lies next to `icons.js` as
  `plugin/CC-BY-4.0-Font-Awesome.txt`; the packages install it with the page and into
  `/usr/share/doc/cockpit-image-update-monitor/`.
- **New name, Image Update Monitor:** the menu, the window title, the header ("Image Update *Monitor*"),
  the signature and AppStream use the same name in every language, the way the design system now treats
  product names; the German "Image-Updates" goes.
- **Package `cockpit-image-update-monitor`:** the package is named like the repository. It conflicts with
  `cockpit-image-updates`, so installing it removes the old package. Paths, data, dumps, settings,
  rollback images and the Diun stack stay. Removing the old package switches the timers of the radar and
  the cleanup off: turn on again what was on (README, Install).
- **Shared design files from Cron Jobs:** `azentrius.css` with `az-page-title--dot` and the
  project-page classes, `icons.js` with the full attribution, `tools/build-packages.sh` with `CONFLICTS`.
  The italic subset now holds the letters of "Monitor".
- **Tables no longer widen the page:** the table wrapper is positioned (`position: relative` on
  `.az-table-wrap`, from the shared `azentrius.css`), so the hidden helper texts in a table head stay inside
  it when the table scrolls sideways.
- **Fix: buttons out of line.** A row without “Change version” (an image whose version is unknown)
  had its Update button further right than the rows above and below, and a history entry with
  “Roll back” but no kept output had its button further right too. A missing icon button now keeps its
  place, invisible and as large as the button; on a phone, where the rows are blocks, the place goes.
- **Fix: dump tick box without a database.** The plan of an update or a version change showed
  “Create database dump first” greyed out when the project has no database to dump. The tick box now
  only appears when there is one, and the run gets `--no-dump` only when it was unticked; before, a
  project without a database got `--no-dump` and the output said the dump was skipped on request.

## 0.12.2 – 2026-10-16

Images that no registry shows are named as such, and nothing is called up to date without a comparison.

- **Fix:** an image built or tagged on the server (`build:` in the compose file, `docker build -t`)
  showed “Registry error” and the status “Up to date”. No registry has it: `hibiscus-server:2.12.4`
  means `docker.io/library/hibiscus-server`, which Docker Hub answers with “requested access to the
  resource is denied”, as it does for private repositories. The radar now gives such images the status
  `unlisted` (also for `unauthorized` and `name unknown`), and the page shows “not in a registry” with
  the reason in the tooltip. Timeouts, rate limits and server errors stay registry errors, now with an
  explanation in the tooltip too. Found on a host with two images built there.
- **Fix:** the status said “Up to date” whenever Diun had reported nothing, even when the radar had
  compared nothing; Docker gives images built locally a digest too. Without a comparison the status is
  now “Unknown”.

## 0.12.1 – 2026-10-16

- **Fix:** taking over an `install.sh` installation left the copies of the timers in
  `/etc/systemd/system` in place, because only copies that name the old helpers were removed and a
  timer names none. Such a copy hides the package's timer, so later changes to it would not arrive.
  A copy that is the same as the package's unit now goes as well, also on hosts already on 0.12.0;
  a timer changed by hand stays and keeps working as an override. Found when switching the
  reference host; CI now installs over a radar timer copy and a changed cleanup timer copy.

## 0.12.0 – 2026-10-16

Packages: the add-on comes as `cockpit-image-updates` (`.deb` and `.rpm`) from the Azentrius package
repository, packages.azentrius.de, and Cockpit lists it under Applications. `install.sh` is gone; the
package takes over its installations.

- **Packages:** every release has `cockpit-image-updates_VERSION-1_all.deb` and
  `cockpit-image-updates-VERSION-1.noarch.rpm` next to the source archive, in `SHA256SUMS` and with a
  signed build provenance. They need `cockpit-bridge` and `python3`; Docker, Diun and skopeo are set up
  from the page as before. The builds are reproducible: CI builds them twice and compares the bytes,
  then installs them on Debian 13 and Fedora 43.
- **Cockpit's Applications page:** the packages carry AppStream data (`de.azentrius.image_updates`,
  English and German). With the package repository set up (one package, `azentrius-repository`),
  Cockpit lists Image updates under Applications and installs it with a click, and the system's updates
  keep it current. The repository takes a package only after checking that this repository's release
  workflow built it.
- **New places:** the page moves from `/usr/share/cockpit/diun` to `/usr/share/cockpit/image-updates`
  (its address in Cockpit ends in `/image-updates` now), the root helpers from
  `/usr/local/lib/diun-radar` to `/usr/lib/cockpit-image-updates`. The units `diun-radar` and
  `diun-cleanup` come with the package in `/usr/lib/systemd/system` and stay off until they are set up;
  *Install* for the radar and the cleanup in the page now switches them on (`systemctl enable --now`),
  and the requirements check asks systemd whether they are on. Data, dumps, backups and
  `/etc/image-updates.conf` stay where they are.
- **Taking over `install.sh`:** the first package install removes the old page, the old helpers and the
  copies of the units in `/etc/systemd/system` that point to them. The `backup-*` folders of
  `install.sh` and all data stay, and timers that were on keep running. CI installs the package over
  what `install.sh` left behind and checks exactly that.
- **Updates and removal:** after every install the package starts a radar run if the radar is set up.
  Removing it stops the timers and removes the page, the helpers and the units; data, dumps, rollback
  images and the Diun stack stay.
- **Other distributions:** `sudo make install` installs the same files to the same places and takes over
  an `install.sh` installation the same way; `sudo make uninstall` removes them.

## 0.11.0 – 2026-10-15

Stage 2: the version of a container can be changed in the page. The radar collects what can be
chosen, the helper shows the lines it will change and makes exactly that change with a byte-exact
backup, and *Change version* offers it with a selection field.

- **Choices per installed version.** Next to the newest patch, minor and major, `versions.json` now
  keeps a short list for every installed version, in its scheme and never older than what runs: the
  running version's own tag (to pin it), the major and minor lines the registry offers (`5`, `5.4`,
  `16-alpine`) with the newest version in each, newer exact versions by level (every newer patch, the
  newest of each newer minor line, the newest of each newer major) and `latest` / `stable` /
  `production` in the image's flavour (`stable-apache`). Pre-releases, other flavours (`fpm` instead
  of `apache`) and `v`-prefixed twins are left out, and so is a line without a released version in
  it (Valkey's `9.2` before `9.2.0`). The running version's own lines always stay, even next to many
  newer ones (ClickHouse `24.3-alpine`).
- **Plan of a tag change.** `diun-update.py retag <container> <tag> --dry-run` (read-only) says what
  the radar offers the tag as, the version it brings and every line that would change, before and
  after: the `image:` line of each service of the image (migration jobs too) or the variable's line in
  the `.env` file. The files are read as text, so comments, quotes and line endings would stay. It
  refuses a tag the radar does not offer, a database to another major, Nextcloud more than one major
  ahead, an image under a YAML anchor and variables that are set twice, only have a default or are
  used elsewhere. Tried read-only on the reference host: every compose file there writes the tag in
  the `image:` line, and GlitchTip's web, worker and migration job change together. The plan carries
  a fingerprint of what it shows.
- **The tag change.** `retag <container> <tag> --expect <fingerprint>` makes the plan again and only
  goes on when it still has the fingerprint the preview showed. Then: dumps, the rollback copy of the
  image, byte-exact copies of the files under `/var/backups/image-updates/compose/<project>/<stamp>/`,
  the planned lines written atomically with the file's owner and mode, and `docker compose config`
  must give every service the new reference. A failed check or a new image that cannot be pulled puts
  the files back before any container changes (exit 5). After that it is an update with the new tag;
  the containers are always recreated, so they carry it. The history records it as a tag change with
  its lines, and the rollback puts the files back, unless one was edited after the change.
- **Change version in the page.** Rows of compose containers whose installed version has choices get
  *Change version* (⇄), and an outlined chip such as “Patch v2.1.5” opens it with its tag. The
  selection field starts on the configured tag and groups the rest: pin the running version, version
  lines (“5, currently 5.4.5”), patch, minor and major versions, and the tags that follow every
  release. Database majors and Nextcloud jumps over more than one major are listed but locked, with
  the reason. Each choice is planned at once: the lines before and after with file and line number,
  hints for a `.env` variable and a git repository, then the plan of an update; a major and a tag that
  follows every release each need a tick box, and a refusal names its reason. *Change version* sends
  the preview's fingerprint, the live output and the rollback work as for an update, and the history
  shows “Tag changed: 5.0.2 → 5”. A rollback that finds an edited file says that nothing changed.
- **Tests:** the choices with tag lists modelled on Formbricks, Nextcloud, PostgreSQL, Valkey and
  ClickHouse (lines, levels, flavours, caps, no pin when the registry lacks the running version's
  tag) and in the radar run; the fixtures carry choices for five images. The tag change with compose
  and `.env` files on disk: direct tags (CRLF, quotes, comments), two services and a job, anchors,
  variables (tag or whole reference, shared, set twice, default only, missing), database and
  Nextcloud majors, the refusals, and that nothing is written. The change itself: fingerprint, the
  written lines (CRLF, quotes, comment, owner, mode), backup, failed check and failed pull restoring
  the files, rollback restoring them byte-exact, an edited file stopping the rollback, the `.env`
  variant, an override file. The page under jsdom: the button and the chips, the groups and locked
  entries, the preview, tick boxes, the last choice winning, refusals, the change with the preview's
  fingerprint, failure and rollback, the history, English without German; the preview builds a
  screenshot of the dialog and checks it on a phone. End to end on a Docker host: a tag change in the
  compose file, one through `.env` and one in an override file, each with its rollback, and the
  refusals for a shared variable and an anchor.

## 0.10.0 – 2026-10-14

Stage 1.2, third part: floating tags resolved. The page says which version `latest` points to, and the
update says what it brings.

- **What a floating tag points to.** For tags without a full version (`latest`, `stable`,
  `16-alpine`, `5` …) the radar looks up the digest the tag has in the registry right now, without a
  pull: the Docker Hub API (no pull quota) or the manifest (`skopeo inspect --raw`). The version tag
  with the same digest is its version. The *Configured* column then reads e.g. “latest = 6.0.2”.
  `versions.json` keeps it as `tag_now` and reuses it while the digest stays the same.
- **Older installed images found.** The first lookup of an installed image outside Docker Hub compares
  the 100 newest version tags instead of 20, so a build several releases behind gets its version
  (Formbricks 5.0.2 was 22nd). A retried miss only looks at the 20 newest.
- **What the update brings.** The plan names the target version: the image pulled already, otherwise
  what the tag points to now. The dialog shows it (“Brings 6.0.2 Major”). A new major version gets a
  warning, and *Update* waits for a tick box (“I have checked the release notes for 6.0.2”). With an
  unknown installed version the existing warning names the target as well.
- **Only versions that count alike are compared.** The column and the plan use what the tag points to
  only when the radar found the installed version among the image's tags. `pgvector/pgvector:pg17`
  runs PostgreSQL 17.10, but its tags count pgvector releases (`pg17` = `0.8.7-pg17-bookworm`): no
  “pg17 = 0.8.7” and no false “17.10 → 0.8.7 Major” warning.
- **More room in the update dialog.** A warning and its tick box stay together, with clear space
  before the plan, the dump box and the notes; the rows of the plan are a little taller, so wrapped
  container names no longer touch.
- **Tests:** the radar with Docker Hub and registry fakes (tag digest, version of the tag, reuse,
  deeper first lookup, lookup errors), the plan (target from the pulled image or `tag_now`, a tag in
  another numbering, jump sizes), the *Configured* column and the dialog (target row, major warning
  and tick box).

## 0.9.0 – 2026-10-13

Stage 1.2, second part: the output of every update stays with the history, a build whose update
failed is not offered silently again, and the update shows what it touches besides its image.

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
- **Fixed:** a container in a restart loop counted as failed only after the full wait (180 seconds).
  Docker counts a restart only when it happens, so the counter can stay 0 while the container is
  restarting. Now a container seen restarting a second time fails the update at once; a single crash
  that recovers still does not. Found by the new end-to-end step on the reference host.
- **What is updated together.** In the overview a row says “together with …” when other running
  containers of the same compose project use the same image: one click on *Update* recreates them too.
  The update dialog groups the containers: recreated (same image) and one-off jobs that run again (e.g.
  `migrate`).
- **Dependencies.** From `docker compose config` the dialog lists what the services wait for
  (`depends_on`, with the condition) and which services wait for them.
  - The services that depend on the updated ones are not restarted, but watched for about 15 seconds
    after the update: they must keep running, without a new restart, and stay healthy. If one does
    not, the update counts as failed, with its cause and the rollback button.
  - A warning when services of the same image do not wait for its one-off job (`depends_on` with
    `condition: service_completed_successfully`): they can start before the migration is done.
  - A hint when other compose projects are on the same user-defined network (e.g. one mail relay for
    several projects); it never blocks anything.
- **Roadmap:** stage 2 plans a selection field for the target version (a fixed version, a version
  line such as `5`, or `latest`).
- **Tests:** offline tests for the kept output (cut at the limit, root only, nothing for an aborted
  update), the `log` verb, the digests of the new build, the output button and the failed-build hint
  with its tick box, the relations in the plan (jobs, `depends_on`, dependents, job order, shared
  networks), the watch of dependent services and the dialog groups. The end-to-end test checks the
  kept output and the digests, and its project has a service that depends on the database and is
  watched.

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
