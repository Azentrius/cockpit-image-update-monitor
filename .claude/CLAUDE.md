# Notes for working on this repository

## What this is

Image updates monitor, a Cockpit add-on: for every running Docker container it shows the configured
tag, the version actually installed and newer versions. Updates with the same tag are applied with a
database dump and a rollback.

- Overview and use: `README.md`
- Interfaces and formats: `docs/INTERNALS.md`
- What comes next: `ROADMAP.md`
- What changed: `CHANGELOG.md`

## Working together

- Big picture first in a few sentences, then details; the recommendation first, then the reasons.
- For anything that runs as root on a server, say briefly what the user has to understand to answer
  for it.

## Repository conventions

The same in every Azentrius repository.

- **Language.** Everything in the repository is English: code, comments, docs, commit messages, pull
  requests, test names and test data. The only German is the German translation and the tests that
  check it. User-facing texts always exist in English and German; German addresses the user formally
  (“Sie”). Answer the user in the language they write in.
- **Versions.** Semantic Versioning; the version lives in exactly one place (see “Releases and the
  repository”). Tags are `vX.Y.Z`.
- **Changelog.** `CHANGELOG.md`, newest first. Each version gets `## X.Y.Z – YYYY-MM-DD`, a one-line
  summary, then bullets. The pull request that raises the version adds its section.
- **Pull requests.** `main` is protected: every change goes through a pull request and is
  squash-merged once the `test` check is green; the branch is deleted afterwards. The PR title becomes
  the commit on `main`: short, imperative, English, without version numbers.
- **Authorship.** Commits on `main` are authored by the maintainer (the squash merge takes care of
  that). Commits, pull requests and files carry no co-author trailers and no “generated with” lines.

Here the German translation is `I18N.de` in `plugin/diun.js`, `plugin/po.manifest.de.js` and the
German half of the helpers' `say(de, en)` / `msg de en` messages.

## Repository baseline

The same in every Azentrius repository; keep it when changing the repository or its settings.

- **Security.** `SECURITY.md` with private vulnerability reporting; secret scanning with push
  protection; Dependabot alerts and security updates; Dependabot version updates for every
  ecosystem in use and for GitHub Actions; CodeQL default setup.
- **Branches and tags.** Ruleset `main`: pull request, squash only, linear history, no deletion or
  force push, required check `test`. Ruleset `release tags`: `v*` cannot be deleted or moved.
- **CI.** The job `test` runs every test and every lint on each push and pull request. Actions are
  pinned by commit SHA with the version as a comment; checkouts use `persist-credentials: false`;
  the token is read-only unless a job needs more. Check tools are pinned as well, so Dependabot keeps
  them current.
- **Releases.** Only the `release` workflow makes releases, on request, from `main`: it checks the
  version against the code and the existing tags, runs all tests, builds the artifact with
  `SHA256SUMS`, signs a build provenance (`gh attestation verify`) and takes the notes from
  `CHANGELOG.md`. Releases are immutable. Files that are not for users stay out of the artifact.
- **Licensing.** Apache-2.0: `LICENSE` in the root, an SPDX header in every source file, `NOTICE`
  for third-party material.

Here: check tools in `test/requirements.txt` (pip) and `test/package.json` (npm); lint is
`shellcheck --severity=warning`, `ruff check` (Python 3.8 syntax, `ruff.toml`) and
`tools/check-units.sh` (`systemd-analyze verify`); the artifact is
`git archive` of the repository minus `export-ignore` (`.gitattributes`).

## Layout

```
plugin/   the Cockpit package, installed to /usr/share/cockpit/diun/   (vanilla JS, NO build step)
          azentrius.*, icons.js, DMSans/DMSerifDisplay-Regular.woff2, OFL.txt: shared design files,
          byte-identical copies of src/ in Azentrius/cockpit-cronjobs (tools/design-files.sha256);
          never edit them here – copy the new versions and update the list
          DMSerifDisplay-Italic.woff2: only the letters of "monitor" (see "Useful knowledge")
lib/      root helpers, installed to /usr/local/lib/diun-radar/
          diunlib.py          shared logic (tag scheme, version variables, reach)
          diun-inventory.py   inventory (on every load of the plugin)
          diun-versions.py    version radar (systemd timer every 6 h) -> /var/lib/diun-radar/versions.json
          diun-update.py      plan / update / rollback / backups / delete-* / cleanup / radar
          diun-deps.sh        check / install skopeo|diun|radar|cleanup / start diun (also holds the Diun stack)
systemd/  unit templates (diun-radar, diun-cleanup), installed to /usr/local/lib/diun-radar/systemd/;
          diun-deps.sh copies them to /etc/systemd/system/ and never replaces existing units
install.sh                    installer (DESTDIR, --dry-run, backs up the previous version)
test/                         offline tests + e2e-server.sh
tools/preview/                the plugin in a browser with test data, screenshots
tools/check-version.sh        version in diun.js = ?v= in index.html = section in CHANGELOG.md
tools/release-notes.sh        notes for a release from CHANGELOG.md
tools/check-units.sh          systemd-analyze verify for systemd/ (part of the CI lint step)
tools/check-design-files.sh   shared design files = tools/design-files.sha256 (part of run-all.sh)
.github/workflows/            test (every push and PR, incl. lint), release (on request)
```

## Commands

```bash
cd test && npm ci && ./run-all.sh              # all offline tests (Python + jsdom), must be green
pip install -r test/requirements.txt           # pinned check tools: shellcheck, ruff, playwright
shellcheck --severity=warning lib/*.sh install.sh test/*.sh tools/*.sh && ruff check .   # lint, as in CI
sh tools/check-units.sh                        # systemd-analyze verify for systemd/, as in CI
python3 tools/preview/build.py                 # preview: tools/preview/out/diun/index.html?lang=de
python3 tools/preview/shoot.py /tmp/shots      # screenshots light, dark, phone (English UI; default docs/img), checks JS errors + phone width
DESTDIR=/tmp/stage ./install.sh                # installer, dry, into a staging directory
```

## Rules (not negotiable)

1. **Root code only in `lib/`**, installed only to `/usr/local/lib/diun-radar` (root-owned).
   - Never run or import anything from `/var/lib/diun`: the Diun container can write there.
2. **Fixed verbs.** The plugin calls helpers only with fixed commands. Every new argument is checked
   in the helper by regex (container names, paths with `realpath`, tags). New verbs also go into
   `docs/INTERNALS.md`.
3. **Show first, then act.** Changing actions: plan (read-only) → confirmation → live output →
   `@@RESULT`.
   - Anything that can fail stops **before** the change (exit 5).
   - Locks with `flock` (exit 75).
4. **Overwrite nothing** the user created (compose files, `/opt/diun`, units). If it cannot be avoided
   (stage 2): a byte-exact backup first, restored on rollback.
5. **Browser without network.** No CDNs, external fonts or external images (the fonts ship with the
   add-on); Cockpit's strict CSP stays.
   Registry and GitHub access only on the server (radar).
6. **Two languages.** Every new text goes into `I18N.en` **and** `I18N.de` in `plugin/diun.js`; helper
   messages through `say(de, en)` / `msg de en`. The tests check that the English UI contains no German.
7. **Formats are contracts.** When the inventory, `versions.json`, `plan`, `backups` or `@@RESULT`
   change, change these together:
   - plugin
   - fixtures in `test/fixtures/`
   - tests
   - `docs/INTERNALS.md`

   Tolerate old fields: radar data can be one version older than the plugin.
8. **Never test on production containers.** End-to-end only through `test/e2e-server.sh` (throw-away
   project `imgupd-test`, cleans up). New flows get a section there.
9. **No build step, no runtime dependencies.** Python standard library only (≥ 3.8), shell POSIX `sh`
   or `bash`, JS without a framework. `node_modules` only for tests.

## Releases and the repository

- The version lives in `const VERSION` in `plugin/diun.js`. `plugin/index.html` loads every script
  and stylesheet of the add-on (the shared design files too) with `?v=<version>` so browsers fetch the
  new files; `tools/check-version.sh` (part of `run-all.sh`) checks that they all agree and that
  `CHANGELOG.md` has a section for the version.
- Release: raise `VERSION` and every `?v=` in `plugin/index.html`, add a section to `CHANGELOG.md`,
  merge, then Actions › release › Run workflow on `main`. The workflow checks the version, runs the
  tests, packs the source as `cockpit-image-update-monitor-<version>.tar.gz` with `SHA256SUMS` and
  creates tag and release.
- Before SemVer the versions were numbered v1–v6; they are 0.1.0–0.6.0 in `CHANGELOG.md`.
- From Claude sessions the GitHub API cannot create tags or releases, delete branches or change
  settings; starting the release workflow works.

## Putting it on a server

1. `cd test && ./run-all.sh` green.
2. On the server: take the release tarball and `SHA256SUMS` and check
   `sha256sum --ignore-missing -c SHA256SUMS` (or update the git checkout), then
   `sudo ./install.sh --dry-run` and `sudo ./install.sh`. The previous version goes to
   `/usr/local/lib/diun-radar/backup-<stamp>/`.
3. If the radar format changed, `install.sh` starts the radar itself. Check the result with
   `journalctl -u diun-radar.service -n 5`.
4. Reload Cockpit in the browser.

## Useful knowledge

- **Docker with the containerd image store:** image ID = index digest. `docker rmi` on the **last** tag
  of an image in use is refused; with `-f` only the tag is removed.
- **Compose labels** the helpers use:
  - `com.docker.compose.project`
  - `.service`
  - `.project.working_dir`
  - `.project.config_files`
  - `.project.environment_file`
  - `.config-hash`
- **Docker Hub API** (`hub.docker.com/v2/repositories/<ns>/<repo>/tags?ordering=last_updated`) returns
  tags with digests and does not count against the pull limit. `skopeo list-tags` is not a pull either,
  `skopeo inspect` is.
- **`diun-radar.service`** runs hardened (`ProtectSystem=strict`, `ProtectHome=yes`). It may write only
  to `/var/lib/diun-radar` and cannot see `/root` (no registry credentials).
- **Cleanup** (`diun-update.py cleanup`, `/etc/image-updates.conf`): after an update it is limited to
  that project and image, so the end-to-end test never deletes other data; the global run is only
  tested with `--dry-run` there. Unknown keys stop it – a new key (e.g. a token for stage 3) has to be
  added to `RULES` or the parser.
- **Design system:** `Azentrius/design-system` (private), `cockpit.md` and `components/`. Only
  “Refresh” is `az-btn--primary` on the page; text buttons carry an icon; tools in rows are icon
  buttons; `az-btn--danger` only confirms deleting. `[hidden]` needs `!important` in `diun.css`,
  because the az classes set `display`. README screenshots come from the CI artifact `screenshots`.
- **Italic subset** (fonttools with brotli or brotlicffi):
  `pyftsubset DMSerifDisplay-Italic.ttf --text="monitor" --flavor=woff2 --output-file=DMSerifDisplay-Italic.woff2`,
  the TTF from google/fonts `ofl/dmserifdisplay/`.
- **Cockpit:**
  - `superuser: "try"` without admin rights gives errors with `problem: "access-denied"`, or exit 77
    from our helpers.
  - `cockpit.file().watch()` fires again on the plugin's own `read()`; that is why it reloads only when
    the tag changed.
