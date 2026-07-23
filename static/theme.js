(function () {
  "use strict";

  var STORAGE_KEY = "as400-monitor-theme";

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "light"
      ? "light"
      : "dark";
  }

  function applyTheme(theme) {
    if (theme === "light") {
      document.documentElement.setAttribute("data-theme", "light");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    updateButton(theme);
  }

  function updateButton(theme) {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    var toLight = theme !== "light";
    btn.querySelector(".theme-icon").textContent = toLight ? "\u2600\uFE0F" : "\uD83C\uDF19";
    btn.querySelector(".theme-label").textContent = toLight ? "Light mode" : "Dark mode";
    btn.setAttribute("aria-label", toLight ? "Switch to light mode" : "Switch to dark mode");
  }

  function toggle() {
    var next = currentTheme() === "light" ? "dark" : "light";
    applyTheme(next);
    try { localStorage.setItem(STORAGE_KEY, next); } catch (e) {}
  }

  document.addEventListener("DOMContentLoaded", function () {
    updateButton(currentTheme());
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.addEventListener("click", toggle);
  });
})();
