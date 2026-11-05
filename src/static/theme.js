/*
 * Theme toggle for minecraft-monitor: cycles System -> Light -> Dark.
 * The choice lives in the "theme" cookie so the server renders the right
 * theme on the next page load (no flash); "System" deletes the cookie and
 * lets the prefers-color-scheme media query decide.
 */
(function () {
  'use strict';

  var MODES = ['system', 'light', 'dark'];
  var ICONS = { system: 'i-auto', light: 'i-sun', dark: 'i-moon' };
  var NAMES = { system: 'System', light: 'Light', dark: 'Dark' };
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function currentMode() {
    var theme = root.getAttribute('data-theme');
    return theme === 'light' || theme === 'dark' ? theme : 'system';
  }

  function nextMode(mode) {
    return MODES[(MODES.indexOf(mode) + 1) % MODES.length];
  }

  function systemTheme() {
    return media && media.matches ? 'dark' : 'light';
  }

  function saveMode(mode) {
    var cookie = 'theme=' + (mode === 'system' ? '' : mode) + '; path=/; SameSite=Lax';
    document.cookie = cookie + (mode === 'system' ? '; max-age=0' : '; max-age=31536000');
  }

  function applyMode(mode) {
    if (mode === 'system') {
      root.removeAttribute('data-theme');
    } else {
      root.setAttribute('data-theme', mode);
    }
  }

  function renderButton(button, mode) {
    var use = button.querySelector('use');
    if (use) {
      use.setAttribute('href', '#' + ICONS[mode]);
    }
    var label = 'Theme: ' + NAMES[mode];
    if (mode === 'system') {
      label += ' (' + systemTheme() + ')';
    }
    button.title = label;
    button.setAttribute('aria-label', label + '. Switch to ' + NAMES[nextMode(mode)].toLowerCase());
  }

  function init() {
    var button = document.getElementById('themeToggle');
    if (!button) {
      return;
    }
    renderButton(button, currentMode());
    button.addEventListener('click', function () {
      var mode = nextMode(currentMode());
      applyMode(mode);
      saveMode(mode);
      renderButton(button, mode);
    });
    if (media && media.addEventListener) {
      media.addEventListener('change', function () {
        renderButton(button, currentMode());
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
