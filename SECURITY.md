# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Report them privately through GitHub instead:
[Report a vulnerability](https://github.com/Azentrius/cockpit-image-update-monitor/security/advisories/new)
(tab **Security → Report a vulnerability** in this repository).
If you cannot use GitHub, write to **support@azentrius.de**.

Please include:

- what is affected (plugin, helper script, systemd unit or package; version or commit),
- steps to reproduce,
- the impact you expect.

Do not send real credentials, tokens, passwords or other private data from your servers. Redact logs,
compose files and command output before you attach them (host names, IP addresses, registry
credentials, database passwords); a made-up example is just as good.

We aim to acknowledge reports within 7 days. This is a small project, so a fix may take longer; we
will keep you informed and credit you in the advisory unless you prefer to stay anonymous. Please keep
the details private until a fixed release is out.

## Supported versions

| Version | Security fixes |
|---|---|
| Newest release | yes |
| Older releases | no |

The add-on is in its 0.x line: only the newest release is supported. Security fixes go into a new
patch or minor release of the latest release line; older releases get no backports. The `main` branch
is a development state, not a supported installation.

To stay on a supported version, upgrade to the newest release:

```bash
# From packages.azentrius.de (see README)
sudo apt update && sudo apt install cockpit-image-update-monitor   # Debian, Ubuntu
sudo dnf upgrade cockpit-image-update-monitor                      # Fedora, RHEL, openSUSE

# From the release assets: check the download, then install it
sha256sum --ignore-missing -c SHA256SUMS
gh attestation verify <file> --repo Azentrius/cockpit-image-update-monitor
```

## How fixes are announced

A fixed vulnerability is announced with:

- a GitHub security advisory in this repository (tab **Security**), with a CVE ID where one is assigned,
- an entry in `CHANGELOG.md` marked as a security fix,
- the release notes of the fixed release, which come from `CHANGELOG.md`.

packages.azentrius.de takes the new release within an hour. To be notified, watch this repository's
releases on GitHub (**Watch › Custom › Releases**).

## Especially relevant

The helpers in `lib/` run as root. Reports are particularly welcome for:

- ways to make the plugin run commands or arguments other than the fixed helper verbs,
- path or container-name handling that escapes the intended scope,
- anything that lets the Diun container or `/var/lib/diun` influence code that runs as root,
- data from registries, Diun events or compose files that reaches the page as markup or script (the
  Cockpit session can act as root),
- dumps, logs or file copies under `/var/backups/image-updates` that others than root can read,
- the package `cockpit-image-update-monitor` (its install and removal scripts) or `make install`
  overwriting or exposing files it should not touch.
