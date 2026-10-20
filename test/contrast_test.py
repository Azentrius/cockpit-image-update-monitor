#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""WCAG contrast of the colour pairs this page uses, in the light and the dark
theme, with the token values from plugin/azentrius.css: text at least 4.5:1,
borders and icons at least 3:1. A pair that diun.css starts to use goes here.
    python3 contrast_test.py"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
css = open(os.path.join(HERE, "..", "plugin", "azentrius.css"), encoding="utf-8").read()


def block(selector):
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", m.group(1)))


THEMES = {"light": block(":root"), "dark": block(":root.pf-v6-theme-dark")}


def lum(hexcol):
    r, g, b = (int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def ch(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# (what, foreground token, background tokens, minimum)
PAIRS = [
    ("label info (text + icon)", "on-tint-info", ["tint-info"], 4.5),
    ("label success (text + icon)", "on-tint-success", ["tint-success"], 4.5),
    ("label warning (text + icon)", "on-tint-warning", ["tint-warning"], 4.5),
    ("label danger (text + icon)", "on-tint-danger", ["tint-danger"], 4.5),
    ("label neutral (text + icon)", "on-tint-neutral", ["tint-neutral"], 4.5),
    ("arrow to the pulled version", "status-info", ["bg-card", "bg-hover"], 4.5),
    ("up to date: check-circle", "status-success", ["bg-card", "bg-hover"], 3.0),
    ("subtle text in cards", "text-subtle", ["bg-card", "bg-hover"], 4.5),
    ("subtle text on the page", "text-subtle", ["bg-page"], 4.5),
    ("danger text in cards / alerts", "danger-text", ["bg-card", "bg-hover"], 4.5),
    ("link (image) in cards", "link", ["bg-card", "bg-hover"], 4.5),
    ("text in cards", "text", ["bg-card", "bg-hover"], 4.5),
    ("Refresh (primary)", "on-primary", ["primary"], 4.5),
    ("outlined button: text", "primary-text", ["bg-card", "bg-hover"], 4.5),
    ("outlined button: border", "primary", ["bg-card", "bg-hover"], 3.0),
    ("icon button (plain)", "text-subtle", ["bg-card", "bg-hover"], 3.0),
    ("danger button (delete)", "on-danger", ["status-danger"], 4.5),
    ("tab selected", "primary-text", ["bg-page"], 4.5),
    ("tab line", "signature", ["bg-page"], 3.0),
    ("tab not selected", "text-subtle", ["bg-page"], 4.5),
    ("title suffix monitor", "signature", ["bg-page"], 4.5),
    ("meta in the header", "text-subtle", ["bg-page"], 4.5),
    ("dialog text", "text", ["bg-floating"], 4.5),
    ("dialog note", "text-subtle", ["bg-floating"], 4.5),
    ("dialog result ok", "status-success", ["bg-floating"], 4.5),
    ("dialog result error", "danger-text", ["bg-floating"], 4.5),
    ("dialog link (Cancel)", "link", ["bg-floating"], 4.5),
    ("dialog log", "text", ["bg-page"], 4.5),
    ("dialog code", "on-tint-neutral", ["tint-neutral"], 4.5),
    ("alert border warning", "status-warning", ["bg-card", "bg-page"], 3.0),
    ("alert border danger", "status-danger", ["bg-card", "bg-page"], 3.0),
    ("alert border info", "status-info", ["bg-card", "bg-page"], 3.0),
    ("alert icon warning", "status-warning", ["bg-card"], 3.0),
    ("alert icon danger", "status-danger", ["bg-card"], 3.0),
    ("alert icon info", "status-info", ["bg-card"], 3.0),
    ("alert border in a dialog: warning", "status-warning", ["bg-floating"], 3.0),
    ("alert border in a dialog: danger", "status-danger", ["bg-floating"], 3.0),
    ("alert border in a dialog: info", "status-info", ["bg-floating"], 3.0),
    ("level as a button: focus ring", "focus", ["bg-card", "bg-hover"], 3.0),
    ("version field: text", "text", ["bg-control"], 4.5),
    ("diff line removed", "on-tint-danger", ["tint-danger"], 4.5),
    ("diff line added", "on-tint-success", ["tint-success"], 4.5),
    ("diff file and line", "text-subtle", ["bg-floating"], 4.5),
    ("release notes: text", "text", ["bg-page"], 4.5),
    ("release notes: marked word", "on-tint-warning", ["tint-warning"], 4.5),
    ("release notes: link to GitHub", "link", ["bg-floating"], 4.5),
    ("spinner arc", "text-subtle", ["bg-card"], 3.0),
    ("empty state: icon", "text-subtle", ["bg-card"], 3.0),
    ("signature dot", "signature", ["bg-page"], 3.0),
]

bad = 0
for theme, tok in THEMES.items():
    print(f"== {theme}")
    for what, fg, bgs, need in PAIRS:
        for bg in bgs:
            r = ratio(tok[fg], tok[bg])
            ok = r >= need
            bad += not ok
            print(f"{'OK  ' if ok else 'LOW '} {r:5.2f} (≥{need})  {what}: {fg} {tok[fg]} on {bg} {tok[bg]}")
print("\nALL PAIRS PASS" if not bad else f"\n{bad} PAIRS TOO LOW")
sys.exit(1 if bad else 0)
