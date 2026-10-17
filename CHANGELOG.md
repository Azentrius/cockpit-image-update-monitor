# Changelog

## Unreleased

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
