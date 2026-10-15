#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Screenshots of the preview in the light and the dark theme, on a desktop
and a phone, e.g. for the README or to check a UI change. The page follows
the browser's colour scheme like Cockpit's "auto" style (azentrius-theme.js).
Needs Playwright with Chromium:

    pip install playwright && python3 -m playwright install chromium
    python3 tools/preview/build.py
    python3 tools/preview/shoot.py [output-dir]      (default: docs/img)

Fails if the page throws a JavaScript error or scrolls horizontally on a
phone-sized screen (in either theme, and in the version dialog).
"""
import os
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PAGE = os.path.join(HERE, "out", "diun", "index.html")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "docs", "img")


def main():
    if not os.path.exists(PAGE):
        sys.exit("Build first: python3 tools/preview/build.py")
    os.makedirs(OUT, exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()

        def page(width, height, lang, scheme="light"):
            pg = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme)
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"file://{PAGE}?lang={lang}")
            pg.wait_for_timeout(500)
            return pg

        pg = page(1440, 900, "en")
        pg.screenshot(path=os.path.join(OUT, "updates.png"))
        pg.click('button[data-update="glitchtip-postgres"]')
        pg.wait_for_timeout(400)
        pg.screenshot(path=os.path.join(OUT, "update-dialog.png"))
        pg.click("#dm-dialog-confirm")
        pg.wait_for_timeout(1200)
        pg.screenshot(path=os.path.join(OUT, "update-done.png"))

        # Change version: the field on a version line, its preview below
        pg = page(1440, 900, "en")
        pg.click('td.az-cell-actions button[data-retag="plausible"]')
        pg.select_option("#dm-retag-select", "v2")
        pg.wait_for_timeout(400)
        pg.screenshot(path=os.path.join(OUT, "version-dialog.png"))

        pg = page(1440, 900, "en", "dark")
        pg.screenshot(path=os.path.join(OUT, "updates-dark.png"))
        pg.click('button[data-update="ollama"]')
        pg.wait_for_timeout(400)
        pg.click("#dm-dialog-confirm")
        pg.wait_for_timeout(1200)
        pg.screenshot(path=os.path.join(OUT, "update-done-dark.png"))

        for scheme, name in (("light", "backups.png"), ("dark", "backups-dark.png")):
            pg = page(1440, 900, "en", scheme)
            pg.click("#dm-tab-btn-backups")
            pg.wait_for_timeout(300)
            pg.screenshot(path=os.path.join(OUT, name))

        widths = {}
        for scheme, name in (("light", "mobile.png"), ("dark", "mobile-dark.png")):
            pg = page(390, 1400, "en", scheme)
            pg.screenshot(path=os.path.join(OUT, name))
            widths[scheme] = pg.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        # The version dialog on a phone: the changed lines wrap instead of scrolling sideways
        pg = page(390, 844, "en")
        pg.click('td.az-cell-actions button[data-retag="plausible"]')
        pg.select_option("#dm-retag-select", "v3.2.1")
        pg.wait_for_timeout(400)
        pg.screenshot(path=os.path.join(OUT, "version-dialog-phone.png"))
        widths["version dialog"] = pg.evaluate("() => { const d = document.getElementById('dm-dialog');"
                                               " return [d.scrollWidth, d.clientWidth]; }")
        browser.close()

    print("Screenshots:", OUT)
    if errors:
        sys.exit("JavaScript errors: " + "; ".join(errors))
    for scheme, (scroll, client) in widths.items():
        if scroll > client:
            sys.exit(f"Horizontal scrolling on the phone ({scheme}): {scroll} > {client}")


if __name__ == "__main__":
    main()
