# Contributing

Bug reports, ideas and pull requests for Image Update Monitor are welcome. Security problems do not
belong in public issues: report them as described in [SECURITY.md](SECURITY.md). For a larger change,
open an issue first so we can agree on the approach; planned work is in [ROADMAP.md](ROADMAP.md).

## Setup

```bash
git clone https://github.com/Azentrius/cockpit-image-update-monitor.git
cd cockpit-image-update-monitor
(cd test && npm ci)                    # jsdom for the plugin tests
pip install -r test/requirements.txt   # pinned check tools: shellcheck, ruff, playwright
```

- The helpers need **Python ≥ 3.8, standard library only**, and `sh` or `bash`. `ruff` checks
  Python 3.8 syntax.
- The plugin is plain JavaScript without a build step.
- Node.js and npm (CI uses Node 22) are needed for the tests only.

## Tests and checks

```bash
make check                       # every offline test: Python helpers, plugin under jsdom, version and changelog
make lint                        # shellcheck, ruff, systemd-analyze verify for the units, as in CI
make packages                    # .deb and .rpm in dist/ (needs dpkg-deb and rpmbuild)
python3 tools/preview/build.py   # the page with test data: tools/preview/out/diun/index.html?lang=en (or de)
```

The offline tests need no Docker, no root and no network. The `test` workflow runs the lint, the
tests and the package build on every push and pull request; it has to be green before a merge.

## End-to-end test

`test/e2e-server.sh` runs the real helpers as root against Docker: it recreates containers, writes
dumps and pulls the Postgres, MariaDB and MySQL images it needs. Run it **only on a throw-away Docker
host or VM, never on a host with production containers.**

```bash
sudo bash test/e2e-server.sh                      # helpers installed in /usr/lib/cockpit-image-updates
sudo E2E_LIB="$PWD/lib" bash test/e2e-server.sh   # the helpers of this checkout, not installed
```

It works in its own compose projects `imgupd-test` and `imgupd-test-my`, touches nothing else and
cleans up on exit. CI runs it on a GitHub runner. A new flow in the helpers gets a section there.

## How the add-on is built

- **Root code only in `lib/`**, installed to `/usr/lib/cockpit-image-updates` (root-owned). Nothing is
  ever run or imported from `/var/lib/diun`: the Diun container can write there.
- **Fixed verbs.** The plugin calls the helpers only with fixed commands. Every new argument is
  checked in the helper (regex, `realpath` for paths). New verbs go into `docs/INTERNALS.md`.
- **Show first, then act.** Every change runs as plan (read-only) → confirmation → live output →
  `@@RESULT`. Anything that can fail stops before the change (exit 5); locks use `flock` (exit 75).
- **Overwrite nothing** the user created. Where it cannot be avoided, a byte-exact backup comes first
  and is restored on rollback.
- **Formats are contracts.** The inventory, `versions.json`, `plan`, `backups` and `@@RESULT` change
  together with the plugin, the fixtures in `test/fixtures/`, the tests and `docs/INTERNALS.md`. Code
  tolerates data one version older: the radar data can come from the previous release.
- **No build step, no runtime dependencies.** Python standard library, POSIX `sh` or `bash`,
  JavaScript without a framework. `node_modules` is for the tests only.
- **Browser without network.** No CDNs, external fonts or images; the fonts ship with the add-on and
  Cockpit's strict CSP stays. Only the radar on the server talks to registries.
- **Shared design files** (`plugin/azentrius.*`, `plugin/icons.js`, the fonts) are byte-identical
  copies from another Azentrius add-on, pinned in `tools/design-files.sha256`. Do not edit them here.

Interfaces, formats and exit codes are described in [docs/INTERNALS.md](docs/INTERNALS.md).

## Requirements for a change

- **English and German.** Every user-facing text goes into `I18N.en` and `I18N.de` in
  `plugin/diun.js`; helper messages go through `say(de, en)` (Python) or `msg de en` (shell). German
  addresses the user formally ("Sie"). The tests check that the English UI contains no German.
- **English in the repository.** Code, comments, docs, commit messages and pull requests are in
  English; the only German is the translation and the tests that check it.
- **Tests** for new behaviour; `make check` and `make lint` green.
- **SPDX header** `SPDX-License-Identifier: Apache-2.0` in every new source file.
- **Changelog.** User-visible changes get an entry under `## Unreleased` in `CHANGELOG.md`. Versions
  and releases are made by the maintainer.
- **No private data.** No secrets, tokens, passwords, registry credentials, host names, IP addresses
  or other real server data in issues, pull requests, tests or fixtures.

## Pull requests

- Open them against `main`. `main` is protected: every change goes through a pull request and is
  squash-merged once the `test` check is green.
- The title becomes the commit on `main`: short, imperative, English, without a version number
  (e.g. "Show the dump size in the plan").
- Keep one topic per pull request and fill in the template: what and why, how it was tested.

## License

Image Update Monitor is licensed under the [Apache License 2.0](LICENSE); contributions are accepted
under the same license. Third-party material keeps its own license and is listed in [NOTICE](NOTICE);
new third-party material needs a compatible license and an entry there.
