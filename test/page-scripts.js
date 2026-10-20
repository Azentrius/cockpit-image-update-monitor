// SPDX-License-Identifier: Apache-2.0
// The page's own scripts for the jsdom tests, in the order index.html loads
// them, from i18n.js on: Cockpit's files are stood in for by each test, and
// the shared design files (icons.js) are evaluated by the tests that need
// them. menu-status.js only when asked for. pageScripts() gives them in one
// piece (window.eval), pageScriptList() one by one as [name, code], to run
// each on its own as the browser does.
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const PLUGIN = path.join(__dirname, "..", "plugin");

function pageScriptList({ menu = false } = {}) {
    const doc = new JSDOM(fs.readFileSync(path.join(PLUGIN, "index.html"), "utf8")).window.document;
    const srcs = [...doc.querySelectorAll("script[src]")].map((s) => s.getAttribute("src").split("?")[0]);
    const from = srcs.indexOf("i18n.js");
    if (from < 0) throw new Error("index.html loads no i18n.js");
    return srcs.slice(from).filter((f) => menu || f !== "menu-status.js")
        .map((f) => [f, fs.readFileSync(path.join(PLUGIN, f), "utf8")]);
}

function pageScripts(opts) {
    return pageScriptList(opts).map(([, code]) => code).join("\n");
}

module.exports = { pageScripts, pageScriptList };
