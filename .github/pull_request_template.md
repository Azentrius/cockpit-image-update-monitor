<!--
Title: short, imperative, English, without a version number. It becomes the commit on main.
Security problems: do not open a pull request, see SECURITY.md.
-->

## What and why

<!-- What does this change, and why? Link the issue if there is one. -->

## How tested

- [ ] `make check`
- [ ] `make lint`
- [ ] `test/e2e-server.sh` on a throw-away Docker host or VM (when the helpers in `lib/` changed)
- [ ] The page in the preview (`python3 tools/preview/build.py`) or in Cockpit (when `plugin/` changed)

<!-- Anything else you tried, and on which system. -->

## Checklist

- [ ] Every new or changed text exists in English and German (`I18N.en` and `I18N.de`, `say(de, en)` / `msg de en`)
- [ ] Changed formats (inventory, `versions.json`, `releases.json`, `plan`, `backups`, `@@RESULT`, `status`) are changed together with the plugin, `test/fixtures/`, the tests and `docs/INTERNALS.md`
- [ ] User-visible changes are described above (the maintainer adds them to the version's section in `CHANGELOG.md`)
- [ ] No secrets, tokens or real server data (host names, IP addresses, credentials) in code, tests, fixtures or this description
- [ ] SPDX header on every new source file
