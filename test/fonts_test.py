#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Every font the page can ask the browser for ships with the add-on.

A browser fetches an @font-face file only for a family that a font stack in
use names. So for every font-family the stylesheets set (custom properties
such as --az-font-code resolved, in the order index.html loads the files),
each family that has an @font-face must have its file in plugin/. A family
whose file lies elsewhere (azentrius.css declares Red Hat Mono from Cockpit's
static files, which Ubuntu 24.04's Cockpit 314 does not ship) must be in no
stack: the page would request a file that may not be there (HTTP 404 in the
real Cockpit session, job cockpit)."""
import os
import re
import sys

PLUGIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin")
fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(("OK   " if ok else "FAIL ") + label + ("" if ok else f"\n     got  {got!r}\n     want {want!r}"))


def stylesheets():
    html = open(os.path.join(PLUGIN, "index.html"), encoding="utf-8").read()
    names = re.findall(r'<link rel="stylesheet" href="([^"?]+)', html)
    return [(n, re.sub(r"/\*.*?\*/", "", open(os.path.join(PLUGIN, n), encoding="utf-8").read(), flags=re.S))
            for n in names]


def families(stack, props, depth=0):
    """The family names of a font stack, var() resolved."""
    out = []
    for part in stack.split(","):
        part = part.strip()
        m = re.fullmatch(r"var\((--[\w-]+)\)", part)
        if m and depth < 10:
            out += families(props.get(m.group(1), ""), props, depth + 1)
        elif part:
            out.append(part.strip("\"'"))
    return out


def analyse(sheets):
    faces, props, stacks = {}, {}, []
    for name, css in sheets:
        for body in re.findall(r"@font-face\s*\{([^}]*)\}", css):
            fam = re.search(r"font-family:\s*([^;]+);", body).group(1).strip().strip("\"'")
            for url in re.findall(r'url\("?([^")]+)"?\)', body):
                faces.setdefault(fam, []).append((name, url))
        rest = re.sub(r"@font-face\s*\{[^}]*\}", "", css)
        for prop, value in re.findall(r"(--font[\w-]*|--az-font[\w-]*)\s*:\s*([^;]+);", rest):
            props[prop] = value.strip()                 # the later file and rule win, as in the browser
        stacks += [v.strip() for v in re.findall(r"(?<![\w-])font-family\s*:\s*([^;]+);", rest)]
    used = {f for s in stacks for f in families(s, props)}
    missing = sorted((fam, url) for fam in used for _, url in faces.get(fam, [])
                     if url.startswith("..") or not os.path.isfile(os.path.join(PLUGIN, url)))
    return used, missing, faces


sheets = stylesheets()
check("the page's stylesheets, in the order index.html loads them",
      [n for n, _ in sheets], ["azentrius.css", "diun.css"])
used, missing, faces = analyse(sheets)
print("     families in use:", ", ".join(sorted(used)))
check("every @font-face family the page uses has its file in plugin/", missing, [])
check("the code font is the system's monospace, not Red Hat Mono", "Red Hat Mono" in used, False)
check("the add-on's own fonts are in use and ship with it",
      sorted(f for f in used if f in faces), ["DM Sans", "DM Serif Display"])
# Counter-check: azentrius.css alone (without diun.css) would request Red Hat Mono from Cockpit.
_, alone, _ = analyse(sheets[:1])
check("counter-check: azentrius.css alone names Red Hat Mono from Cockpit's files",
      alone, [("Red Hat Mono", "../../static/fonts/RedHatMono/RedHatMonoVF.woff2")])

print("\nALL CHECKS PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
