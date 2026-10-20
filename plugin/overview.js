// SPDX-License-Identifier: Apache-2.0
/* Image Update Monitor, a Cockpit add-on: the Updates tab: Diun's events, the
 * inventory and the radar's data read into rows, the columns and the status of
 * each row, and the meta line. Part of the page, see common.js. */
(function () {
    "use strict";
    const P = window.ImageUpdatesPage;
    if (!P) return;     // common.js did not load: diun.js asks Cockpit for it again
    const { t, icon, $, tagSplit, repoKey, verKey, evKey, shortDigest, esc, fmtDay, fmtDate, ago,
            ciJobText, label, textButton, iconButton, iconSlot, LEVELS } = P;

    const RADAR_STALE_H = 24;   // radar older than this -> flagged in the header

    // --- Parsing -----------------------------------------------------------
    // Diun's newest event per image and tag (verKey: "postgres:16"), and a
    // registry link per image. A row takes only the event of its own tag: an
    // event for postgres:18 says nothing about postgres:16. An event for a
    // reference with a digest (postgres@sha256:…, also a CI job's service
    // container) belongs to no row: a pinned image does not follow its tag,
    // so the Update button could not bring what Diun saw.
    function parseEvents(content) {
        const byTag = new Map();
        const links = new Map();
        (content || "").split("\n").forEach((line) => {
            line = line.trim();
            if (!line) return;
            let e;
            try { e = JSON.parse(line); } catch (_) { return; }
            if (!e.image) return;
            if (e.hub_link) links.set(repoKey(e.image), e.hub_link);
            if (e.image.includes("@")) return;
            const tg = tagSplit(e.image);
            const k = verKey(tg.repo, tg.tag);
            const prev = byTag.get(k);
            if (!prev || (e.ts || "") >= (prev.ts || "")) byTag.set(k, e);
        });
        return { byTag, links };
    }
    function parseInventory(stdout) {
        const out = [];
        (stdout || "").split("\n").forEach((line) => {
            line = line.trim();
            if (!line) return;
            try { out.push(JSON.parse(line)); } catch (_) { /* skip */ }
        });
        return out;
    }
    function parseVersions(doc) {
        const map = new Map();
        const images = (doc && Array.isArray(doc.images)) ? doc.images : [];
        images.forEach((r) => {
            if (r && r.image) map.set(verKey(r.image, r.tag), r);
        });
        return map;
    }

    // --- Rendering: updates table ------------------------------------------
    function badge(kind) {
        const [cls, ic, key] = STATUS[kind] || STATUS.unknown;
        return label(cls, ic, t(key), kind === "pulled" ? t("badge.pulledTip") : "");
    }

    // Status language of the design system: colour, icon and word say the
    // same; gold is never a status. kind "" = without judgement.
    const STATUS = {
        update: ["info", "info-circle", "badge.update"],
        pulled: ["info", "info-circle", "badge.pulled"],
        newver: ["info", "info-circle", "badge.newver"],
        ok: ["success", "check-circle", "badge.ok"],
        unknown: ["", "ban", "badge.unknown"],
    };

    // Installed version of a container, first known source wins:
    //   inventory: the tag itself or the image's version variable (always fresh)
    //   radar:     image label or digest comparison with the registry (every 6 h)
    // A line tag (cubejs/cube:v1.6, 24.12-alpine) names only its line: the
    // radar's version wins when it refines the tag's number (1.6 -> 1.6.74).
    const refines = (found, v) => !!found && (!v || found.startsWith(v + "."));
    function installedOf(ctn, rec) {
        const r = rec && rec.installed && ctn.imgid ? rec.installed[ctn.imgid] : null;
        const fromTag = (ctn.version_src || "tag") === "tag";
        if (ctn.version && !(fromTag && r && refines(r.version, ctn.version))) {
            return { v: ctn.version, src: ctn.version_src || "tag" };
        }
        if (r && r.version) return { v: r.version, src: r.src, tag: r.tag };
        return { v: null, src: null, looked: !!r };
    }
    function pulledVersionOf(ctn, rec) {
        const r = rec && rec.installed && ctn.tagid ? rec.installed[ctn.tagid] : null;
        const found = r && r.version ? r.version : null;
        return refines(found, ctn.pulled_version) ? found : ctn.pulled_version || found;
    }
    function sourceText(inst) {
        const s = inst.src || "";
        if (s === "tag") return t("inst.tag");
        if (s.indexOf("env:") === 0) return t("inst.env", { name: s.slice(4) });
        if (s === "label") return t("inst.label");
        if (s === "digest") return t("inst.digest", { tag: inst.tag || inst.v });
        return "";
    }

    // A source revision as people quote it: 7 characters of a commit hash,
    // anything else as it is (at most 12 characters).
    function shortRevision(rev) {
        const r = String(rev || "").trim();
        return /^[0-9a-f]{7,64}$/i.test(r) ? r.slice(0, 7) : r.slice(0, 12);
    }

    // "Installed" column: exact version + where it comes from (tooltip);
    // "→ 16.15" when a newer image is already pulled but not rolled out.
    // Without a version, a development build that names its source revision
    // shows it: "main @35b2a97" (pomerium:main on a test host, 2026-10-18).
    // An image built on this server (rec.base) names no version of its own:
    // the version its final base brings (caddy:2 -> 2.11.6), as the radar read
    // it from the build's variables, stands in. Only shown: the row's status
    // follows the bases (baseCell).
    function installedCell(ctn, rec, inst) {
        const rev = !inst.v && ctn.revision ? shortRevision(ctn.revision) : "";
        const base = !inst.v && !rev && rec && Array.isArray(rec.base) ? rec.base.find((b) => b.final && b.version) : null;
        const baseRef = base ? `${base.image}:${base.tag}` : "";
        const lines = [inst.v ? sourceText(inst)
            : rev ? t("inst.revision", { rev: ctn.revision })
            : base ? t("inst.base", { image: baseRef })
            : t(inst.looked ? "inst.unknown" : "inst.pending")];
        if (rev && ctn.created && !isNaN(new Date(ctn.created))) lines.push(t("inst.built", { date: fmtDate(new Date(ctn.created)) }));
        if (ctn.digest) lines.push(t("inst.digestLine", { d: shortDigest(ctn.digest) }));
        const title = esc(lines.filter(Boolean).join("\n"));
        let html = inst.v
            ? `<span class="dm-ver" title="${title}">${esc(inst.v)}</span>`
            : rev ? `<span class="dm-ver" title="${title}">${esc(ctn.tag)} @${esc(rev)}</span>`
            : base ? `<span class="dm-ver" title="${title}">${esc(base.version)}</span>` +
                     ` <span class="dm-muted dm-small dm-base-note">${esc(t("inst.baseNote", { image: baseRef }))}</span>`
            : `<span class="dm-muted" title="${title}">?</span>`;
        const pv = ctn.pulled ? pulledVersionOf(ctn, rec) : null;
        if (pv && pv !== inst.v) {
            html += ` <span class="dm-arrow" title="${esc(t("inst.pulledTip", { v: pv }))}">→ ${esc(pv)}</span>`;
        }
        return `<span class="dm-inst">${html}</span>`;
    }

    // "Configured" column: what a floating tag (latest, 16-alpine) points to
    // in the registry right now, from the radar ("tag_now"). Only when that
    // counts like the installed version: pgvector:pg17 runs PostgreSQL 17.10,
    // its tags count pgvector releases (0.8.7-pg17).
    function tagNowNote(ctn, rec, inst) {
        const v = rec && rec.tag_now && rec.tag_now.version;
        const cmp = inst.v && rec && rec.versions ? rec.versions[inst.v] : null;
        if (!v || (inst.v && (!cmp || cmp.status === "unknown"))) return "";
        return ` <span class="dm-tagnow" title="${esc(t("cfg.tagNow", { tag: ctn.tag, v }))}">= ${esc(v)}</span>`;
    }

    // "Newest version" column. Returns { html, news, compared }:
    //   news = an actionable newer version exists (patch/minor, or a major
    //          on a non-database image). A database major alone is grey info.
    //   compared = the radar compared the installed version (or the tag) with
    //          the registry's tags; not for an image no registry shows
    //          ("unlisted") or a registry error without earlier data.
    // Each level (patch, minor, major) is a label with the level as a word,
    // compared with the INSTALLED version. data-reach says how to get it:
    //   update    the configured tag delivers it via the Update button: a hint
    //             (az-label--info, info-circle)
    //   tag       needs a different tag: without judgement (az-label)
    //   migration a database major: without judgement, with ban
    // A "tag" level the selection field offers (pick) sits in a button that
    // opens "Change version" with that tag. Gold is never a status.
    const REACH = { update: ["az-label az-label--info", "info-circle"], tag: ["az-label", ""],
                    migration: ["az-label", "ban"] };

    // An image built on this server (compose `build:`): no registry shows it,
    // but the radar checks the images its Dockerfile starts FROM (rec.base).
    // A base published after the build, or with a newer version than the
    // build carries, is news; the tooltip says how to rebuild. With every base
    // older than the build the row is up to date as far as the radar can tell.
    function baseCell(ctn, rec, muted) {
        const ref = (b) => `${b.image}:${b.tag}`;
        const lines = [t("ver.baseBuilt", { date: fmtDay(rec.built) })];
        rec.base.forEach((b) => {
            if (b.pinned) { lines.push(t("ver.basePinned", { ref: ref(b) })); return; }
            if (b.error) { lines.push(t("ver.baseError", { ref: ref(b), error: b.error })); return; }
            lines.push(t("ver.baseLine", { ref: ref(b), role: t(b.final ? "ver.baseFinal" : "ver.baseStage"),
                                          now: b.now || "?", pushed: fmtDay(b.pushed) }) +
                       (b.version ? t("ver.baseCarries", { version: b.version }) : "") +
                       (b.newer ? t("ver.baseIsNewer") : ""));
        });
        const checked = rec.base.filter((b) => !b.pinned && !b.error);
        const newer = checked.filter((b) => b.newer);
        if (newer.length) {
            const first = newer.find((b) => b.final) || newer[0];
            const files = ctn.files || [];
            const dir = files.length ? files[0].slice(0, files[0].lastIndexOf("/")) || "/" : "?";
            lines.push(t("ver.baseRebuild", { service: ctn.service || "?", dir }));
            if (ctn.managed) lines.push(t("ver.baseRebuildBtn"));
            const text = first.now ? t("ver.baseNewerNow", { ref: ref(first), now: first.now }) : t("ver.baseNewer", { ref: ref(first) });
            return { html: `<span class="az-label az-label--info dm-level" data-level="base" title="${esc(lines.join("\n"))}">` +
                           `${icon("info-circle")}${esc(text)}</span>`, news: true, compared: true, baseChecked: true };
        }
        if (!checked.length && rec.base.some((b) => b.error)) return { ...muted(t("ver.baseUnchecked"), lines.join("\n")), compared: false };
        lines.push(t("ver.baseCurrentTip"));
        return { html: `<span class="dm-uptodate" title="${esc(lines.join("\n"))}">${icon("check-circle")}` +
                       `<span>${esc(t("ver.baseCurrent"))}</span></span>`, news: false, compared: true, baseChecked: true };
    }

    // A tag like latest that no release tag shares a digest with, pushed to
    // Docker Hub after the newest release (radar: tag_now.after; MobSF's latest
    // is a 4.5.4 build while v4.5.3 is the newest release). Only while that
    // release is still the newest the radar knows.
    function buildAfter(rec) {
        const tn = rec.tag_now;
        const a = tn && !tn.version && tn.after;
        return a && a.tag && a.tag === rec.newest ? a : null;
    }
    // Data a radar run that could not reach the registry kept from the run
    // before (keep_last_good: stale, error, checked): "(older data)", its
    // tooltip the error and when the data was found.
    function staleNote(rec) {
        if (!rec || !rec.stale) return "";
        const when = rec.checked && !isNaN(new Date(rec.checked)) ? fmtDate(new Date(rec.checked)) : null;
        const tip = t("ver.staleTip", { error: String(rec.error || "?").replace(/[.\s]+$/, "") }) +
                    (when ? " " + t("ver.staleSince", { date: when }) : "");
        return ` <span class="dm-muted dm-stale" title="${esc(tip)}">${esc(t("ver.stale"))}</span>`;
    }
    // Nothing newer for over a year (ronmi/forgejo-mcp: latest built
    // 2025-10-28): the newest there is, but old, and the project may be
    // abandoned. A hint below the cell; the status stays up to date. A fixed
    // date (1970, reproducible builds) proves nothing.
    const ABANDONED_DAYS = 365;
    function ageNote(ctn) {
        const built = builtAt(ctn && ctn.created);
        if (built === null || built <= Date.UTC(1980, 0, 1) / 1000) return "";
        if (Date.now() / 1000 - built < ABANDONED_DAYS * 86400) return "";
        return `<span class="dm-row-note dm-age" title="${esc(t("ver.oldTip", { date: fmtDay(ctn.created) }))}">${esc(t("ver.old"))}</span>`;
    }

    // What the build cells say about the build the container runs: with stale
    // data only what the last good run found, no claim that nothing is newer.
    function runsLine(ctn, rec, same) {
        return t(!same ? "ver.buildAfterOther" : rec.stale ? "ver.sameBuildStale" : "ver.buildAfterRuns", { cfg: ctn.tag });
    }
    function buildCell(ctn, text, tip, same, rec, muted) {
        const stale = staleNote(rec);
        if (!same) {
            const m = muted(text, tip);
            return { ...m, html: m.html + stale, compared: false };
        }
        return { html: `<span class="dm-uptodate" title="${esc(tip)}">${icon("check-circle")}<span>${esc(text)}</span></span>${stale}` +
                       ageNote(ctn),
                 news: false, compared: true, sameBuild: true };
    }

    // "build after v4.5.3": up to date (check-circle) when the container runs
    // the build the tag points to now; the version number stays unknown.
    function buildAfterCell(ctn, rec, after, muted) {
        const same = !!ctn.digest && ctn.digest === rec.tag_now.digest;
        const text = t("ver.buildAfter", { tag: after.tag });
        const tip = [t("ver.buildAfterTip", { cfg: ctn.tag, tag: after.tag, pushed: fmtDay(rec.tag_now.pushed),
                                              released: fmtDay(after.pushed) }),
                     runsLine(ctn, rec, same)].join("\n");
        return buildCell(ctn, text, tip, same, rec, muted);
    }

    // A tag like latest in a registry without any release tag: an own image
    // built from its main branch (ghcr.io/azentrius/sharewerk, 2026-10-19).
    // Up to date (check-circle) when the container runs the build the tag
    // points to now. Only Docker Hub tells when a tag was pushed, so with
    // release tags in the registry this stays "newest: X" and unknown.
    // The build date stays in sight: the newest build of an abandoned image
    // (ronmi/forgejo-mcp, latest from 2025-10-28) is up to date but old.
    // A fixed date (1970, reproducible builds) is left out.
    function newestBuildCell(ctn, rec, muted) {
        const same = !!ctn.digest && ctn.digest === rec.tag_now.digest;
        const tip = [t("ver.newestBuildTip", { cfg: ctn.tag }), runsLine(ctn, rec, same)].join("\n");
        const built = builtAt(ctn.created);
        const text = !same ? t("ver.otherBuild")
            : built !== null && built > Date.UTC(1980, 0, 1) / 1000
                ? t("ver.newestBuildOn", { date: fmtDay(ctn.created) }) : t("ver.newestBuild");
        return buildCell(ctn, text, tip, same, rec, muted);
    }

    function versionCell(ctn, rec, inst, radarOk, pick) {
        const muted = (text, title) => ({
            html: `<span class="dm-muted"${title ? ` title="${esc(title)}"` : ""}>${esc(text)}</span>`,
            news: false
        });
        if (!radarOk) return muted("—", t("ver.noRadar"));
        if (!rec) return muted("—", t("ver.notCovered"));
        let cmp = inst.v && rec.versions ? rec.versions[inst.v] : null;
        if (!cmp || cmp.status === "unknown") cmp = rec.by_tag || null;   // pg17 -> pg18
        if (!cmp) {
            const detail = (key, vars) => [t(key, vars), rec.error].filter(Boolean).join("\n");
            if (rec.status === "unlisted" && Array.isArray(rec.base) && rec.base.length) return baseCell(ctn, rec, muted);
            if (rec.status === "unlisted") return muted(t("ver.unlisted"), detail("ver.unlistedTip", { image: ctn.image }));
            if (rec.status === "error") return muted(t("ver.registryError"), detail("ver.registryErrorTip"));
            const after = buildAfter(rec);
            if (after) return buildAfterCell(ctn, rec, after, muted);
            const tn = rec.tag_now;
            if (!rec.newest && tn && tn.digest && !tn.version) return newestBuildCell(ctn, rec, muted);
            if (rec.newest) return muted(t("ver.newestOnly", { v: rec.newest }), t("ver.newestOnlyTip"));
            return muted("—", t("ver.notCovered"));
        }
        const stale = staleNote(rec);
        const reach = Array.isArray(rec.reach) ? rec.reach : [];
        const levels = [];
        let news = false;
        LEVELS.forEach(([lvl, label]) => {
            if (!cmp[lvl]) return;
            const info = lvl === "major" && rec.db;
            if (!info) news = true;
            const can = reach.indexOf(lvl) > -1;
            const how = info ? "migration" : can ? "update" : "tag";
            const [cls, ic] = REACH[how];
            const tag = how === "tag" && pick && pick.has(cmp[lvl]) ? cmp[lvl] : null;
            const title = esc([info ? t("ver.dbMajor") : t("ver.levelTip", { level: label }),
                               t(can ? "ver.reachYes" : "ver.reachNo", { tag: ctn.tag })]
                .concat(tag ? [t("ver.clickRetag", { tag })] : []).join("\n"));
            const lab = `<span class="${cls} dm-level" data-level="${lvl}" data-reach="${how}"${tag ? "" : ` title="${title}"`}>` +
                        `${ic ? icon(ic) : ""}${label} <span class="dm-ver">${esc(cmp[lvl])}</span></span>`;
            levels.push(tag
                ? `<button type="button" class="dm-level-btn" title="${title}" ` +
                  `data-retag="${esc(ctn.name)}" data-tag="${esc(tag)}">${lab}</button>`
                : lab);
        });
        if (!levels.length) {
            return { html: `<span class="dm-uptodate" title="${esc(t("ver.upToDateTip"))}">${icon("check-circle")}` +
                           `${inst.v ? `<span class="dm-ver">${esc(inst.v)}</span>` : ""}</span>${stale}${ageNote(ctn)}`,
                     news: false, compared: true };
        }
        return { html: `<span class="dm-levels">${levels.join("")}</span>${stale}`, news, compared: true };
    }

    // Failed builds from the history: image ref -> the newest update of that
    // image, kept only if it failed. Its digests mark the same build when
    // Diun reports it again (see sameFailedBuild).
    let failedBuilds = new Map();
    const rowFailed = new Map();        // container name -> failed update op
    const siblings = new Map();         // container name -> others an update recreates with it
    const retagInfo = new Map();        // container name -> { ctn, inst, options } for "Change version"
    const rowRec = new Map();           // container name -> its radar record (release notes)
    let lastRender = null;

    function failedBuildsOf(ops) {
        const map = new Map();
        const seen = new Set();
        (ops || []).forEach((o) => {     // newest first
            if (o.kind !== "update" || !o.image || o.status === "running") return;
            const tg = tagSplit(o.image);
            const k = verKey(tg.repo, tg.tag);
            if (seen.has(k)) return;
            seen.add(k);
            if (o.status === "failed") map.set(k, o);
        });
        return map;
    }

    function sameFailedBuild(ctn, ev) {
        const fb = failedBuilds.get(verKey(ctn.image, ctn.tag));
        return fb && ev && ev.digest && (fb.new_digests || []).indexOf(ev.digest) > -1 ? fb : null;
    }

    // The history arrives after the table; draw the rows again if it changes
    // which builds failed.
    function refreshFailedBuilds(ops) {
        const next = failedBuildsOf(ops);
        const sig = (m) => JSON.stringify([...m.entries()].map(([k, o]) => [k, o.id]));
        if (sig(next) === sig(failedBuilds)) return;
        failedBuilds = next;
        if (lastRender) render(...lastRender);
    }

    // When an image was built, in seconds: Docker's "2026-10-15T19:00:50.089Z"
    // and Diun's "2026-10-15 19:00:50.089 +0000 UTC". null if unreadable.
    function builtAt(s) {
        const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?/.exec(s || "");
        if (!m) return null;
        const zone = !m[3] || m[3] === "Z" ? "Z" : m[3].slice(0, 3) + ":" + m[3].slice(-2);
        const ms = Date.parse(m[1] + "T" + m[2] + zone);
        return isNaN(ms) ? null : ms / 1000;
    }
    // Diun reports a new digest of the tag, but the build is the one that runs:
    // Docker Hub publishes the list of platforms of an official image again
    // when it adds one (nextcloud:apache, 2026-10-16: riscv64), and its digest
    // changes while the amd64 build stays. A fixed date (1970, reproducible
    // builds) proves nothing.
    function sameBuild(ev, ctn) {
        const a = builtAt(ev.created), b = builtAt(ctn.created);
        return a !== null && a === b && a > Date.UTC(1980, 0, 1) / 1000;
    }

    function rowFor(ctn, events, versionsMap, radarOk) {
        const ev = events.byTag.get(verKey(ctn.image, ctn.tag));
        const acknowledged = ev && P.isDismissed(evKey(ev));
        let kind;
        if (ctn.pulled) {
            kind = "pulled";        // newer image already local, container on the old one
        } else if (ev && ev.digest && ctn.digest && ev.digest !== ctn.digest && !acknowledged && !sameBuild(ev, ctn)) {
            kind = "update";
        } else if (!ctn.digest) {
            kind = "unknown";
        } else {
            kind = "ok";
        }
        const rec = versionsMap.get(verKey(ctn.image, ctn.tag));
        rowRec.set(ctn.name, rec || null);
        const inst = installedOf(ctn, rec);
        const options = ctn.managed ? P.retagOptions(ctn, rec, inst) : [];
        const pick = new Set(options.filter((o) => !o.disabled).map((o) => o.tag));
        if (pick.size) retagInfo.set(ctn.name, { ctn, inst, options });
        const ver = versionCell(ctn, rec, inst, radarOk, pick);
        if ((kind === "ok" || kind === "unknown") && ver.news) kind = "newver";
        // Without a known installed version, or when the radar compared
        // nothing (no registry shows the image, a registry error), "up to
        // date" would be a guess: Docker gives locally built images a digest
        // too, and Diun reports nothing for them. Not a guess: the container
        // runs the build a tag like latest points to now (sameBuild).
        if (kind === "ok" && radarOk && rec && ((!inst.v && !ver.sameBuild && !ver.baseChecked) || !ver.compared)) kind = "unknown";
        const failedBuild = kind === "update" ? sameFailedBuild(ctn, ev) : null;
        rowFailed.set(ctn.name, failedBuild);
        const note = failedBuild
            ? `<span class="dm-row-note">${esc(t("row.failedBuild", { date: fmtDate(new Date(failedBuild.ts)) }))}</span>` : "";

        const link = events.links.get(repoKey(ctn.image));
        const imgCell = link
            ? `<a href="${esc(link)}" target="_blank" rel="noopener">${esc(ctn.image)}</a>`
            : esc(ctn.image);

        // Three places: the update, "Change version", "Dismiss" (see iconSlot).
        // An image built on this server with a newer base: "Rebuild" in the
        // update's place (`rebuild`, the update with a build instead of the
        // pull; an update refuses such an image).
        let main = "";
        if (ver.baseChecked && ver.news && ctn.managed) {
            main = textButton("arrow-circle-up", t("btn.rebuild"), `data-update="${esc(ctn.name)}" data-kind="rebuild"`);
        } else if (kind === "update" || kind === "pulled") {
            main = ctn.managed
                ? textButton("arrow-circle-up", t(kind === "pulled" ? "btn.rollout" : "btn.update"),
                             `data-update="${esc(ctn.name)}" data-kind="${kind}"`)
                : `<span class="dm-muted dm-small" title="${esc(t("row.manualOnlyTip"))}">${esc(t("row.manualOnly"))}</span>`;
        }
        const retag = pick.size ? iconButton("exchange-alt", t("btn.retag"), `data-retag="${esc(ctn.name)}"`) : "";
        const dismissBtn = kind === "update" ? iconButton("check", t("btn.dismiss"), `data-key="${esc(evKey(ev))}"`) : "";
        const actions = main || retag || dismissBtn ? [main, iconSlot(retag), iconSlot(dismissBtn)] : [];

        const sib = siblings.get(ctn.name) || [];
        const together = sib.length
            ? `<span class="dm-row-note">${esc(t("row.together", {
                names: sib.slice(0, 3).join(", ") + (sib.length > 3 ? " +" + (sib.length - 3) : "") }))}</span>`
            : "";

        const L = (key) => esc(t(key));
        return { kind, html:
            `<td data-label="${L("col.container")}"><span class="dm-cell"><span class="dm-name">${esc(ctn.name)}</span>${together}</span></td>` +
            `<td data-label="${L("col.image")}" class="dm-img"><span class="dm-cell az-mono">${imgCell}</span></td>` +
            `<td data-label="${L("col.configured")}"><span class="dm-cell"><span class="dm-ver dm-cfg" title="${L("cfg.tip")}">${esc(ctn.tag)}</span>${tagNowNote(ctn, rec, inst)}</span></td>` +
            `<td class="az-nowrap" data-label="${L("col.installed")}"><span class="dm-cell">${installedCell(ctn, rec, inst)}</span></td>` +
            `<td data-label="${L("col.new")}"><span class="dm-cell">${ver.html}</span></td>` +
            `<td data-label="${L("col.status")}"><span class="dm-cell">${badge(kind)}${note}</span></td>` +
            `<td class="az-cell-actions"><span class="dm-cell">${actions.join("")}</span></td>`
        };
    }

    const ORDER = { update: 0, pulled: 1, newver: 2, unknown: 3, ok: 4 };

    function radarMeta(radarDoc) {
        if (!radarDoc || !radarDoc.generated) return t("meta.radarNone");
        const a = ago(radarDoc.generated);
        if (!a) return t("meta.radarBad");
        return t("meta.radar", { ago: a.text }) + (a.hours > RADAR_STALE_H ? " " + t("meta.outdated") : "") +
            (noRegistry(radarDoc) ? " " + t("meta.noRegistry") : "");
    }
    // The radar's last run reached no registry, not even on its second try
    // (versions.json "no_registry"): the server's DNS or network, mostly.
    function noRegistry(radarDoc) {
        const nr = radarDoc && radarDoc.no_registry;
        return nr && typeof nr === "object" ? nr : null;
    }

    // fromEvents: no inventory, the rows come from Diun's events alone.
    function render(all, events, radarDoc, fromEvents) {
        lastRender = [all, events, radarDoc, fromEvents];
        // Containers of CI jobs come and go with the job and cannot be
        // updated: counted in the meta line, not listed (ci_job, inventory).
        const ciJobs = all.filter((c) => c.ci_job);
        const containers = all.filter((c) => !c.ci_job);
        rowFailed.clear();
        retagInfo.clear();
        rowRec.clear();
        // An update recreates every service of the project with the same image
        // that was started from the same compose files.
        siblings.clear();
        const groups = new Map();
        containers.forEach((c) => {
            if (!c.project) return;
            const k = c.project + "|" + (c.files || []).join(",") + "|" + verKey(c.image, c.tag);
            if (!groups.has(k)) groups.set(k, []);
            groups.get(k).push(c.name);
        });
        groups.forEach((names) => names.forEach((n) => {
            const others = names.filter((x) => x !== n).sort();
            if (others.length) siblings.set(n, others);
        }));
        const rows = $("dm-rows");
        rows.innerHTML = "";

        const radarOk = !!(radarDoc && Array.isArray(radarDoc.images));
        const versionsMap = parseVersions(radarDoc);
        const built = containers.map((c) => ({ c, ...rowFor(c, events, versionsMap, radarOk) }));
        built.sort((a, b) =>
            (ORDER[a.kind] - ORDER[b.kind]) || a.c.name.localeCompare(b.c.name));

        const updates = built.filter((b) => b.kind === "update" || b.kind === "pulled").length;
        const newver = built.filter((b) => b.kind === "newver").length;
        $("dm-meta").textContent = [
            t("meta.updates", { n: updates }),
            t("meta.newver", { n: newver }),
            t("meta.containers", { n: containers.length }),
        ].concat(ciJobs.length ? [t("meta.ciJobs", { n: ciJobs.length })] : [], [radarMeta(radarDoc)]).join(" · ");
        const nr = noRegistry(radarDoc);
        $("dm-meta").title = (nr ? [t("meta.noRegistryTip", { error: String(nr.error || "?").replace(/[.\s]+$/, "") })] : [])
            .concat(ciJobs.length ? [t("meta.ciJobsTip")].concat(ciJobs.map((c) => ciJobText(c.name, null))) : [])
            .join("\n");

        if (containers.length === 0) {
            $("dm-table").hidden = true;
            $("dm-legend").hidden = true;
            $("dm-empty-title").textContent = t(fromEvents ? "empty.eventsTitle" : "empty.title");
            $("dm-empty-body").textContent = t(fromEvents ? "empty.eventsBody" : "empty.body");
            $("dm-empty").hidden = false;
            P.scrollToPlace();
            return;
        }
        $("dm-empty").hidden = true;
        $("dm-table").hidden = false;
        $("dm-legend").hidden = !radarOk;

        built.forEach((b) => {
            const tr = document.createElement("tr");
            tr.dataset.kind = b.kind;
            tr.innerHTML = b.html;
            rows.appendChild(tr);
        });

        rows.querySelectorAll("button[data-key]").forEach((btn) => {
            btn.addEventListener("click", () => P.dismiss(btn.dataset.key));
        });
        rows.querySelectorAll("button[data-update]").forEach((btn) => {
            btn.addEventListener("click", () => P.startUpdate(btn.dataset.update, btn.dataset.kind));
        });
        // The row's "Change version" and the outlined chips (with their tag).
        rows.querySelectorAll("button[data-retag]").forEach((btn) => {
            btn.addEventListener("click", () => P.startRetag(btn.dataset.retag, btn.dataset.tag || ""));
        });
        P.scrollToPlace();
    }

    Object.assign(P, { parseEvents, parseInventory, render, refreshFailedBuilds, rowFailed, retagInfo, rowRec,
                       rendered: () => lastRender !== null });
})();
