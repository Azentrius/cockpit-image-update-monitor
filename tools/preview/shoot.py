#!/usr/bin/env python3
"""Screenshots of the preview (desktop + phone), e.g. for the README or to
check a UI change. Needs Playwright with Chromium:

    pip install playwright && python3 -m playwright install chromium
    python3 tools/preview/build.py
    python3 tools/preview/shoot.py [output-dir]      (default: docs/img)

Fails if the page throws a JavaScript error or scrolls horizontally on a
phone-sized screen.
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

        def page(width, height, lang):
            pg = browser.new_page(viewport={"width": width, "height": height})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"file://{PAGE}?lang={lang}")
            pg.wait_for_timeout(500)
            return pg

        pg = page(1440, 900, "de")
        pg.screenshot(path=os.path.join(OUT, "updates.png"))
        pg.click('button[data-update="glitchtip-postgres"]')
        pg.wait_for_timeout(400)
        pg.screenshot(path=os.path.join(OUT, "update-dialog.png"))
        pg.click("#dm-dialog-confirm")
        pg.wait_for_timeout(1200)
        pg.screenshot(path=os.path.join(OUT, "update-done.png"))

        pg = page(1440, 900, "de")
        pg.click("#dm-tab-btn-backups")
        pg.wait_for_timeout(500)
        pg.screenshot(path=os.path.join(OUT, "backups.png"))

        pg = page(1440, 900, "en")
        pg.screenshot(path=os.path.join(OUT, "updates-en.png"))

        pg = page(390, 1400, "de")
        pg.screenshot(path=os.path.join(OUT, "mobile.png"))
        widths = pg.evaluate("() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
        browser.close()

    print("Screenshots:", OUT)
    if errors:
        sys.exit("JavaScript errors: " + "; ".join(errors))
    if widths[0] > widths[1]:
        sys.exit(f"Horizontal scrolling on the phone: {widths}")


if __name__ == "__main__":
    main()
