# Security Policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Report them privately through GitHub instead:
[Report a vulnerability](https://github.com/Azentrius/cockpit-image-update-monitor/security/advisories/new)
(tab **Security → Report a vulnerability** in this repository).

Please include:

- what is affected (plugin, helper script, installer; version or commit),
- steps to reproduce,
- the impact you expect.

We aim to acknowledge reports within 7 days. This is a small project, so a fix
may take longer; we will keep you informed and credit you in the advisory
unless you prefer to stay anonymous.

## Supported versions

Only the latest state of the `main` branch receives security fixes.

## Especially relevant

The helpers in `lib/` run as root. Reports are particularly welcome for:

- ways to make the plugin run commands or arguments other than the fixed helper verbs,
- path or container-name handling that escapes the intended scope,
- anything that lets the Diun container or `/var/lib/diun` influence code that runs as root,
- the installer (`install.sh`) overwriting or exposing files it should not touch.
