#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The add-on in a real Cockpit session, on a throw-away machine with systemd and
Docker (CI, test workflow, job cockpit; never on a server with real
containers). Playwright drives Chromium through Cockpit's own login and shell:

  0. Cockpit before the add-on is installed: the errors the browser shows
     for Cockpit itself (on Ubuntu e.g. its */po.js), as the baseline;
  1. the release before this build installed (when there is one), the page
     opened as an administrator: its version;
  2. this build installed over it with apt, the page reloaded in the same
     session, then a new login: the new version and its texts (i18n.js);
  3. the page loaded hidden after the login ("preload"), only its status icon
     in the menu set, nothing else started; then found in Cockpit's menu and
     loaded; at the end, no JavaScript error and no failed request of the
     add-on's files or frame in any session with this build, and no error of
     another kind than the baseline's (the release before is reported apart:
     its errors are its own, this build may have fixed them);
  4. requirements from the page: skopeo installed, the radar set up
     (diun-radar.timer on, a first run), then "Run radar now" (a new run);
     the menu's status icon as `diun-update.py status` says;
  5. the compose project of test/cockpit-setup.sh: "Roll out image", its
     plan, the live output, the result, the container on the new image; then
     the rollback from the history, the container on the old image again;
     after each, the menu's status icon again; then the Status Board's links
     (cockpit.jump to a place of the loaded page: its tab, no reload);
  6. a user without administrative access (German browser): the status icon
     in German before the page is opened, then the hint, no actions.

Environment: ADMIN_USER, ADMIN_PW (sudo group), PLAIN_USER, PLAIN_PW, NEW_DEB
(this build), NEW_VERSION, OLD_DEB and OLD_VERSION (empty without an older
release), APP, OLD_ID, NEW_ID (test/cockpit-setup.sh). Writes screenshots,
report.json and report.md to the folder given; exit 1 if a step failed.
"""
import json
import os
import re
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

URL = "https://localhost:9090"
E = os.environ
OUT = sys.argv[1] if len(sys.argv) > 1 else "cockpit-session"
FRAME = 'iframe[name$="/image-updates"]'
APP = E["APP"]
NEED_ADMIN = {"Checking requirements needs administrative access in Cockpit.": "en",
              "Zum Prüfen der Voraussetzungen wird administrativer Zugriff in Cockpit benötigt.": "de"}
os.makedirs(OUT, exist_ok=True)

report = {"environment": {}, "steps": [], "errors": [], "addon_files": []}
failed = False
errors = []          # [kind, text, url, user, phase]
PHASE = {"now": "baseline"}  # baseline, release before, upgrade (the reload right after it), this build
OURS = "/image-updates/"  # the add-on's files and frame (the shell's route has no slash after it)
responses = []       # [url, status] of the add-on's own files
shots = 0
LAST = {"page": None}  # the page in use, for a screenshot when a step fails


def note(name, ok, detail=""):
    global failed
    failed |= not ok
    report["steps"].append({"step": name, "ok": bool(ok), "detail": detail})
    print(("OK   " if ok else "FAIL ") + name + (f" ({detail})" if detail else ""), flush=True)
    return ok


def root(*cmd, check=True):
    p = subprocess.run(["sudo", "env", "DEBIAN_FRONTEND=noninteractive", *cmd], capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"{' '.join(cmd)} -> {p.returncode}\n{p.stdout}\n{p.stderr}")
    return p.stdout.strip()


def shot(page, name):
    global shots
    shots += 1
    page.screenshot(path=os.path.join(OUT, f"{shots:02d}-{name}.png"), full_page=True)


def watch(page, who):
    page.on("console", lambda m: m.type == "error" and errors.append(
        ["console", m.text, (m.location or {}).get("url", ""), who, PHASE["now"]]))
    page.on("pageerror", lambda e: errors.append(["pageerror", f"{e.message}\n{e.stack or ''}", "", who, PHASE["now"]]))
    page.on("requestfailed", lambda r: errors.append(["requestfailed", r.failure or "", r.url, who, PHASE["now"]]))

    def response(r):
        if "/image-updates/" in r.url:
            responses.append([r.url, r.status])
        if r.status >= 400:
            errors.append([f"http {r.status}", "", r.url, who, PHASE["now"]])
    page.on("response", response)


BASELINE = set()
STALE = []           # errors of the reload after the upgrade that are Cockpit's old list of files


def new_files(old_deb):
    """The page's files this build brings that the release before did not have."""
    page_dir = "/usr/share/cockpit/image-updates/"
    before = {line.split()[-1].split(page_dir, 1)[1] for line in root("dpkg-deb", "-c", old_deb).splitlines()
              if page_dir in line}
    return set(os.listdir(page_dir)) - before


REFUSED_RE = re.compile(r"Refused to execute script from '([^']+)'")


def missed_new_file(e, fresh):
    """The new file an error says Cockpit did not deliver, or "": its 404, the failed
    request, and the console's "Failed to load resource" and "Refused to execute script
    from '…'" (Cockpit's 404 is an HTML page, which the browser does not run)."""
    refused = REFUSED_RE.search(e[1]) if e[0] == "console" else None
    if refused:
        url = refused.group(1)
    elif e[0] in ("http 404", "requestfailed") or (e[0] == "console" and "Failed to load resource" in e[1]):
        url = e[2] or e[1].split(" ")[0]
    else:
        return ""
    name = url.split("/image-updates/", 1)[-1].split("?")[0] if "/image-updates/" in url else ""
    return name if name in fresh else ""


def broken_off(e):
    """A request of the add-on's files the browser broke off (net::ERR_ABORTED): the page
    loading itself again, or the session closed, while it still loaded fonts or styles."""
    return e[0] == "requestfailed" and "ERR_ABORTED" in e[1] and OURS in e[2]


def kind(e):
    """An error without where it was seen: the same message on another route
    of the shell is the same error."""
    return (e[0], e[1] if e[0] != "console" else e[1].split(" because ")[0], e[2] if e[0] != "console" else "")


def poll(page, fn, seconds):
    end = time.monotonic() + seconds
    while True:
        v = fn()
        if v or time.monotonic() > end:
            return v
        page.wait_for_timeout(250)


def login(browser, user, pw, locale="en-US"):
    ctx = browser.new_context(ignore_https_errors=True, locale=locale, viewport={"width": 1440, "height": 1000})
    page = ctx.new_page()
    page.set_default_timeout(90000)
    watch(page, user)
    page.goto(URL + "/", wait_until="domcontentloaded")
    page.fill("#login-user-input", user)
    page.fill("#login-password-input", pw)
    page.click("#login-button")
    LAST["page"] = page
    # Logged in (the shell loads a page in a frame), or Cockpit says why not.
    page.wait_for_selector("iframe, #login-error-message:not(:empty)", state="attached")
    if not page.locator("iframe").count():
        raise RuntimeError(f"login of {user} refused: " + page.locator("#login-error-message").inner_text())
    return ctx, page


def admin_access(page, pw):
    """Administrative access: Cockpit gets it with the login password; if the
    shell shows "Limited access", switch it on with the password."""
    btn = page.get_by_role("button", name=re.compile(r"^(Limited|Administrative) access$"))
    btn.first.wait_for()
    if "Limited" in btn.first.inner_text():
        btn.first.click()
        dlg = page.get_by_role("dialog")
        dlg.locator("input[type=password]").fill(pw)
        dlg.get_by_role("button", name=re.compile("Authenticate")).click()
    page.get_by_role("button", name="Administrative access").wait_for()
    return True


def open_page(page):
    """Through Cockpit's menu; the frame once its first load is done."""
    link = page.locator('a[href$="/image-updates"]').first
    link.wait_for()
    label = link.inner_text().strip()
    link.click()
    frame = page.frame_locator(FRAME)
    frame.locator("#dm-sig-version").wait_for()
    poll(page, lambda: frame.locator("#dm-loading").is_hidden(), 120)
    poll(page, lambda: "Image Update Monitor " in (frame.locator("#dm-sig-version").text_content() or ""), 60)
    return frame, label


def version_of(frame):
    return (frame.locator("#dm-sig-version").text_content() or "").replace("Image Update Monitor", "").strip()


def run_dialog(page, frame, opener, seconds):
    """Open a dialog, confirm it once the plan is there (ticking the boxes a
    plan may ask for), follow the output: (title, result, ok, live, log).
    live: output was on the page before the result was."""
    opener.click()
    confirm = frame.locator("#dm-dialog-confirm")

    def ready():
        for g in frame.locator("#dm-dialog-body .dm-gate").all():
            if g.is_enabled() and not g.is_checked():
                g.check()
        return confirm.is_visible() and confirm.is_enabled()
    if not poll(page, ready, 120):
        raise RuntimeError("the dialog offers no confirm: " + (frame.locator("#dm-dialog").text_content() or ""))
    title = (frame.locator("#dm-dialog-title").text_content() or "").strip()
    confirm.click()
    seen = {"live": False}

    def result():
        text = (frame.locator("#dm-dialog-result").text_content() or "").strip()
        if not text and (frame.locator("#dm-dialog-log").text_content() or "").strip():
            seen["live"] = True
        return text
    text = poll(page, result, seconds)
    ok = "is-ok" in (frame.locator("#dm-dialog-result").get_attribute("class") or "")
    log = frame.locator("#dm-dialog-log").text_content() or ""
    return title, text, ok, seen["live"], log


def close_dialog(page, frame):
    frame.locator("#dm-dialog-cancel").click()
    poll(page, lambda: not frame.locator("#dm-dialog").evaluate("d => d.hasAttribute('open')"), 30)


MENU_TYPES = {"danger": "error", "warning": "warning", "info": "info"}   # Cockpit's page status types


def shown_status(page):
    """The status the page sent the shell for its menu entry (the shell keeps
    every page's in the session storage); "unset" before the first."""
    stored = json.loads(page.evaluate("() => sessionStorage.getItem('cockpit:page_status')") or "{}")
    return stored.get("localhost", {}).get("image-updates", "unset")


def check_menu(page, lang, what):
    """The icon and its tooltip as `diun-update.py status` says: its state
    (ok and none: no icon) and its summary."""
    doc = json.loads(root("/usr/lib/cockpit-image-updates/diun-update.py", "status", "--lang", lang))
    want = {"type": MENU_TYPES[doc["state"]], "title": doc["summary"]} if doc["state"] in MENU_TYPES else None
    icon = page.locator('a[href$="/image-updates"] .nav-item-status')
    poll(page, lambda: shown_status(page) == want and icon.count() == (1 if want else 0), 30)
    got = shown_status(page)
    return note(f"{what}: the status icon in the menu as `status --lang {lang}` says",
                got == want and icon.count() == (1 if want else 0),
                f"state {doc['state']}, sent {json.dumps(got, ensure_ascii=False)}, icons {icon.count()}")


def check_preloaded(page, lang, what):
    """Right after the login, on another page: the add-on's page loaded hidden,
    its menu status sent, and nothing else of it started (diun.js writes the
    version into the signature when it starts)."""
    page.wait_for_selector(FRAME, state="attached")
    poll(page, lambda: shown_status(page) != "unset", 60)
    page.wait_for_timeout(2000)
    sig = (page.frame_locator(FRAME).locator("#dm-sig-version").text_content() or "").strip()
    note(f"{what}: the page loaded hidden for the menu, not started",
         not page.locator(FRAME).is_visible() and shown_status(page) != "unset" and sig == "Image Update Monitor",
         f"route {page.url.split('9090', 1)[-1]}, signature '{sig}'")
    check_menu(page, lang, what)


def check_places(page, frame):
    """The Status Board's links: it jumps with cockpit.jump("/image-updates#/<place>"), and Cockpit
    keeps a loaded page and only changes its location. The page shows each place's tab without
    loading again, and a tab chosen on the page goes into the shell's address, so the link to the
    place shown before leads there again."""
    own = next(f for f in page.frames if f.name.endswith("/image-updates"))
    own.evaluate("() => { window.imgupdNotReloaded = true; }")

    def at(place, tab, how):
        shown = frame.locator(f'#dm-tab-btn-{tab}[aria-selected="true"]')
        ok = poll(page, lambda: shown.count() == 1 and page.url.endswith("/image-updates#" + place), 30)
        note(f"Status Board link {how}: the tab {tab}, the page not loaded again",
             bool(ok) and own.evaluate("() => window.imgupdNotReloaded === true"),
             f"address {page.url.split('9090', 1)[-1]}")

    for place, tab in (("/updates/rollout", "updates"), ("/backups/history", "backups")):
        own.evaluate("p => cockpit.jump(p)", "/image-updates#" + place)
        at(place, tab, "#" + place)
    frame.locator("#dm-tab-btn-updates").click()
    at("/updates", "updates", "(the tab chosen on the page goes into the address)")
    own.evaluate("p => cockpit.jump(p)", "/image-updates#/backups/history")
    at("/backups/history", "backups", "#/backups/history again")


def radar_generated():
    try:
        return json.loads(root("cat", "/var/lib/diun-radar/versions.json")).get("generated")
    except (RuntimeError, ValueError):
        return None


def image_of(name):
    return root("docker", "inspect", "-f", "{{.Image}}", name)


def environment(browser):
    env = report["environment"]
    env["system"] = root("sh", "-c", ". /etc/os-release && echo \"$PRETTY_NAME\"") + ", kernel " + root("uname", "-r")
    env["cockpit"] = root("dpkg-query", "-W", "-f", "${Version}", "cockpit-ws")
    env["cockpit-bridge"] = root("dpkg-query", "-W", "-f", "${Version}", "cockpit-bridge")
    env["systemd"] = root("systemctl", "--version").splitlines()[0]
    env["docker"] = root("docker", "version", "--format", "{{.Server.Version}}")
    env["compose"] = root("docker", "compose", "version", "--short")
    env["python3"] = root("python3", "--version")
    env["browser"] = f"{browser.browser_type.name} {browser.version}"
    env["release before"] = E.get("OLD_VERSION") or "none"
    env["this build"] = E["NEW_VERSION"]


def main():
    old, new = E.get("OLD_DEB", ""), E["NEW_DEB"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        environment(browser)

        # 0. Cockpit on its own: the baseline of errors that are not the add-on's.
        ctx, page = login(browser, E["ADMIN_USER"], E["ADMIN_PW"])
        admin_access(page, E["ADMIN_PW"])
        page.goto(URL + "/system")
        page.wait_for_selector("iframe", state="attached")
        page.wait_for_timeout(3000)
        BASELINE.update(kind(e) for e in errors)
        note("Cockpit without the add-on: login, administrative access, the shell",
             not page.locator('a[href$="/image-updates"]').count(),
             f"{len(BASELINE)} kinds of browser errors from Cockpit itself")
        ctx.close()

        # 1. + 2. The release before, then this build over it: reload, new login.
        if old:
            PHASE["now"] = "release before"
            root("apt-get", "install", "-y", "-q", old)
            ctx, page = login(browser, E["ADMIN_USER"], E["ADMIN_PW"])
            note("administrator: administrative access", admin_access(page, E["ADMIN_PW"]))
            frame, label = open_page(page)
            note("release before: the page in the menu and loaded", version_of(frame) == E["OLD_VERSION"],
                 f"menu '{label}', version {version_of(frame)}")
            shot(page, "release-before")
            root("apt-get", "install", "-y", "-q", new)
            note("this build installed over it (apt)",
                 root("dpkg-query", "-W", "-f", "${Version}", "cockpit-image-update-monitor").startswith(E["NEW_VERSION"]),
                 root("dpkg-query", "-W", "-f", "${Version}", "cockpit-image-update-monitor"))
            # Cockpit's bridge keeps the list of the package's files it read first in this session;
            # files new in this build answer 404 until it reads the packages again. The shell asks
            # for that when it starts, while it loads the page: the page may miss them, asks the
            # bridge itself (cockpit.Packages.Reload) and loads once more (2026-10-20, this race).
            # Those 404s are Cockpit's; anything else stays an error.
            PHASE["now"] = "upgrade"
            page.reload()
            frame = page.frame_locator(FRAME)
            frame.locator("#dm-sig-version").wait_for()
            poll(page, lambda: version_of(frame) == E["NEW_VERSION"], 60)
            fresh = new_files(old)
            missed = sorted({missed_new_file(x, fresh) for x in errors if x[4] == "upgrade"} - {""})
            note("after the upgrade: reloaded in the same session, the new version", version_of(frame) == E["NEW_VERSION"],
                 version_of(frame) + ("; Cockpit answered 404 for " + ", ".join(missed) +
                                      " until the page asked it to read the packages again" if missed else ""))
            ctx.close()     # still the upgrade: what closing breaks off belongs to it
            STALE.extend(x for x in errors if x[4] == "upgrade" and (missed_new_file(x, fresh) or broken_off(x)))
            PHASE["now"] = "this build"
        else:
            PHASE["now"] = "this build"
            root("apt-get", "install", "-y", "-q", new)
            note("no older release to upgrade from: this build installed fresh (apt)", True)

        ctx, page = login(browser, E["ADMIN_USER"], E["ADMIN_PW"])
        note("new login: administrative access", admin_access(page, E["ADMIN_PW"]))
        check_preloaded(page, "en", "new login")
        frame, label = open_page(page)
        note("new login: the page in the menu and loaded, this build's version",
             label == "Image Update Monitor" and version_of(frame) == E["NEW_VERSION"],
             f"menu '{label}', version {version_of(frame)}")
        texts = [u for u, s in responses if "/i18n.js?v=" + E["NEW_VERSION"] in u and s == 200]
        note("the texts (i18n.js) of this build loaded, the page in English",
             bool(texts) and (frame.locator("#dm-refresh .dm-btn-label").text_content() or "").strip() == "Refresh",
             texts[0].split("/")[-1] if texts else "i18n.js not loaded")
        row = frame.locator("#dm-rows tr", has_text=APP)
        poll(page, lambda: row.count() == 1, 60)
        rollout = row.locator(f'button[data-update="{APP}"]')
        note("inventory: the compose project's container, its newer image pulled but not rolled out",
             row.count() == 1 and rollout.count() == 1 and rollout.inner_text().strip() == "Roll out image",
             (row.first.inner_text() if row.count() else "no row").replace("\n", " | ")[:200])
        shot(page, "overview")

        # 4. Requirements from the page: skopeo, the radar, a radar run.
        skopeo = frame.locator('button[data-verb="install"][data-target="skopeo"]')
        if skopeo.count():
            title, text, ok, live, log = run_dialog(page, frame, skopeo, 600)
            note("skopeo installed from the page", ok and "skopeo installed." in log, f"{title} -> {text}")
            close_dialog(page, frame)
        else:
            note("skopeo offered for installation", False,
                 "no install button; skopeo here: " + (root("sh", "-c", "command -v skopeo || echo none", check=False)))
        radar = frame.locator('button[data-verb="install"][data-target="radar"]')
        poll(page, lambda: radar.count() == 1, 60)
        title, text, ok, live, log = run_dialog(page, frame, radar, 900)
        first = radar_generated()
        timer = (root("systemctl", "is-enabled", "diun-radar.timer", check=False),
                 root("systemctl", "is-active", "diun-radar.timer", check=False))
        note("radar set up from the page: timer on, a first run wrote versions.json",
             ok and timer == ("enabled", "active") and bool(first), f"{title} -> {text}; timer {timer}; generated {first}")
        close_dialog(page, frame)
        frame.locator("#dm-refresh").click()
        poll(page, lambda: (frame.locator("#dm-meta").text_content() or "").find("Radar") >= 0
             and "no data yet" not in (frame.locator("#dm-meta").text_content() or ""), 60)
        meta = (frame.locator("#dm-meta").text_content() or "").strip()
        note("the page shows the radar's data", "Radar" in meta and "no data yet" not in meta, meta[:160])
        time.sleep(1.5)        # versions.json stamps whole seconds: the next run gets a later one
        frame.locator("#dm-radar").click()
        second = poll(page, lambda: (radar_generated() or first) != first and radar_generated(), 900)
        poll(page, lambda: frame.locator("#dm-radar").is_enabled(), 120)
        note("Run radar now: a new radar run, the button usable again",
             bool(second) and frame.locator("#dm-radar").is_enabled(), f"{first} -> {second}")
        # The release notes for the dialogs: written by the hardened unit next to versions.json.
        found = json.loads(root("cat", "/var/lib/diun-radar/releases.json", check=False) or "null")
        owner = root("stat", "-c", "%a %U", "/var/lib/diun-radar/releases.json", check=False)
        note("the radar wrote releases.json next to versions.json, readable for the page",
             isinstance(found, dict) and isinstance(found.get("repos"), dict) and owner == "644 root",
             f"{owner}; {len((found or {}).get('repos') or {})} sources")
        runs = root("journalctl", "-u", "diun-radar.service", "-o", "cat", "--no-pager", check=False)
        note("journal: the radar runs finished", runs.count("Finished") >= 2 or runs.count("Deactivated successfully") >= 2,
             f"{runs.count('Finished')} finished")
        # After an install the package starts the radar two minutes later, by a timer: the same apt
        # run can restart the resolver or the network (build-host, 2026-10-20). It no longer
        # restarts the running radar timer either, which started the radar at once (OnBootSec=).
        # The radar timer starts runs of its own, too: 5 minutes after the boot (OnBootSec=; this
        # runner booted minutes ago, and once that run began in the very second of the check) and
        # on its calendar. RandomizedDelaySec=10min holds for both: the boot run comes anywhere
        # from 5 to 15 minutes after the boot (its time is in NextElapseUSecMonotonic, the
        # calendar's in NextElapseUSecRealtime). configure's daemon-reload draws that delay anew,
        # so a run that has not come yet can land in the past and start at once (up 381 s, with
        # the boot run still more than two minutes away before configure). Wait until the boot run
        # is over, the next calendar run is further away than the delay plus two minutes and no
        # run is going, so that a run right after configure can only come from configure.
        def boot_run_in(up):
            """Seconds until the timer's run after the boot, or None if it has none left."""
            mono = root("systemctl", "show", "-P", "NextElapseUSecMonotonic", "diun-radar.timer", check=False).strip()
            if not mono or mono in ("infinity", "0"):
                return None
            span = root("systemd-analyze", "timespan", mono, check=False)
            m = re.search(r"s:\s*(\d+)\s*$", span, re.M)
            return int(m.group(1)) / 1e6 - up if m else 0

        def radar_quiet():
            up = float(root("cat", "/proc/uptime").split()[0])
            nxt = root("systemctl", "show", "-P", "NextElapseUSecRealtime", "diun-radar.timer", check=False)
            due = root("date", "-d", nxt, "+%s", check=False) if nxt and nxt != "n/a" else ""
            idle = root("systemctl", "is-active", "diun-radar.service", check=False) not in ("activating", "active")
            return idle and up > 5 * 60 + 20 and boot_run_in(up) is None \
                and (not due.isdigit() or int(due) - time.time() > 10 * 60 + 120)
        waited = time.monotonic()
        quiet = poll(page, radar_quiet, 1020)
        waited = round(time.monotonic() - waited)
        uptime = root("cat", "/proc/uptime").split()[0]
        root("/var/lib/dpkg/info/cockpit-image-update-monitor.postinst", "configure", check=False)
        later = root("systemctl", "is-active", "diun-radar-after-install.timer", check=False)
        busy = root("systemctl", "is-active", "diun-radar.service", check=False)
        timer = root("systemctl", "is-active", "diun-radar.timer", check=False)
        root("systemctl", "stop", "diun-radar-after-install.timer", check=False)
        note("after an install: the radar starts two minutes later by a timer, not at once, its timer runs on",
             quiet and later == "active" and busy not in ("activating", "active") and timer == "active",
             f"after-install timer {later}, radar {busy}, radar timer {timer}; "
             f"no timer run due or going before ({quiet}, waited {waited} s), up {uptime} s")
        check_menu(page, "en", "after the radar runs")
        shot(page, "radar")

        # 5. Roll out the pulled image, then roll it back from the history.
        rollout = frame.locator(f'button[data-update="{APP}"]')
        title, text, ok, live, log = run_dialog(page, frame, rollout, 600)
        now = image_of(APP)
        note("roll out: plan, live output, result, the container on the new image",
             ok and live and text.startswith("Update successful") and "+ docker compose" in log
             and "@@RESULT" not in log and now == E["NEW_ID"],
             f"{title} -> {text}; live output {live}; image {now[:19]}")
        shot(page, "rolled-out")
        close_dialog(page, frame)
        check_menu(page, "en", "after the roll-out")
        frame.locator("#dm-tab-btn-backups").click()
        op_row = frame.locator("#dm-ops tr", has_text=APP).first
        op_row.wait_for()
        title, text, ok, live, log = run_dialog(page, frame, op_row.locator("button[data-op]"), 600)
        now = image_of(APP)
        note("rollback from the history: result, the container on the old image again",
             ok and text == "Rollback successful." and now == E["OLD_ID"], f"{title} -> {text}; image {now[:19]}")
        shot(page, "rolled-back")
        close_dialog(page, frame)
        check_menu(page, "en", "after the rollback")
        check_places(page, frame)
        ctx.close()

        # 6. Without administrative access (a user without sudo, German browser).
        ctx, page = login(browser, E["PLAIN_USER"], E["PLAIN_PW"], locale="de-DE")
        check_preloaded(page, "de", "without administrative access, German")
        shot(page, "menu-status")
        frame, label = open_page(page)
        poll(page, lambda: (frame.locator("#dm-deps-intro").text_content() or "").strip() in NEED_ADMIN, 60)
        hint = (frame.locator("#dm-deps-intro").text_content() or "").strip()
        note("without administrative access: the page loads, says what it needs, offers no action",
             hint in NEED_ADMIN and frame.locator("button[data-update], button[data-verb]").count() == 0
             and version_of(frame) == E["NEW_VERSION"],
             f"language {NEED_ADMIN.get(hint, '?')}: {hint}")
        shot(page, "no-admin")
        ctx.close()
        browser.close()


try:
    main()
except Exception as e:      # a step that could not even run counts as failed
    note("the session ran to its end", False, f"{type(e).__name__}: {e}"[:600])
    try:
        LAST["page"] and shot(LAST["page"], "failed")
    except Exception:
        pass

build = [x for x in errors if x[4] in ("upgrade", "this build") and x not in STALE]
before = [x for x in errors if x[4] == "release before" and kind(x) not in BASELINE]
ours = [x for x in build if OURS in x[1] + x[2]]
note("no JavaScript error and no failed request of the add-on's files or frame", not ours,
     "; ".join(f"{k} {u} {t[:120]}" for k, t, u, _, _ in ours)[:600])
other = [x for x in build if x not in ours and kind(x) not in BASELINE]
note("no other error in Cockpit than those it shows without the add-on", not other,
     "; ".join(f"{k} {u} {t[:120]}" for k, t, u, _, _ in other)[:600]
     or f"{len(build)} errors with this build, all of kinds Cockpit shows on its own")
report["release_before_errors"] = sorted({f"{k} {u or t[:100]}" for k, t, u, _, _ in before})
report["errors"] = errors
report["addon_files"] = sorted({u.split("/image-updates/", 1)[1] + f" {s}" for u, s in responses})
with open(os.path.join(OUT, "report.json"), "w") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
md = ["## Image Update Monitor in a real Cockpit session", "", "| | |", "|---|---|"]
md += [f"| {k} | {v} |" for k, v in report["environment"].items()]
md += ["", "| Step | Result |", "|---|---|"]
md += [f"| {s['step']} | {'ok' if s['ok'] else 'FAILED'}{': ' + s['detail'] if s['detail'] else ''} |"
       for s in report["steps"]]
md += ["", "Errors with the release before, beyond the baseline (its own, not this build's): "
       + ("; ".join(report["release_before_errors"]) or "none")]
md += ["", "Errors Cockpit shows on its own, before the add-on is installed (the baseline): "
       + ("; ".join(sorted({f"{k} {u or t[:80]}" for k, t, u in BASELINE})) or "none")]
with open(os.path.join(OUT, "report.md"), "w") as f:
    f.write("\n".join(md).replace("\n|", "\n|") + "\n")
print("\n".join(md))
sys.exit(1 if failed else 0)
