/* Azentrius: follows Cockpit's light/dark setting (user menu › Style).
   Loaded in <head> before the stylesheet, so that nothing flashes light.
   SPDX-License-Identifier: Apache-2.0 */
(function () {
    "use strict";
    var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

    function stored() {
        try { return window.localStorage.getItem("shell:style"); } catch (e) { return null; }
    }

    function apply(style) {
        style = style || stored() || "auto";
        var dark = style === "dark" || (style === "auto" && !!media && media.matches);
        document.documentElement.classList.toggle("pf-v6-theme-dark", dark);
    }

    window.addEventListener("storage", function (e) { if (e.key === "shell:style") apply(); });
    window.addEventListener("cockpit-style", function (e) { apply(e.detail && e.detail.style); });
    if (media) {
        if (media.addEventListener) media.addEventListener("change", function () { apply(); });
        else if (media.addListener) media.addListener(function () { apply(); });
    }
    apply();
})();
