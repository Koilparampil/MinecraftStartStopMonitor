/*
 * Login page helpers (minecraft-monitor).
 * - A reload never re-posts the login form (the page may be a POST result).
 * - While locked out, count the remaining seconds down and re-enable the
 *   form when the lockout ends, so the page never stays "locked".
 */
(function () {
  'use strict';

  function init() {
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, '', window.location.href);
    }
    var form = document.querySelector('form[data-lockout-seconds]');
    if (!form) return;
    var button = form.querySelector('button[type="submit"]');
    var banner = document.getElementById('loginError');
    var text = banner ? banner.querySelector('span') : null;
    var end = Date.now() + (Number(form.getAttribute('data-lockout-seconds')) || 0) * 1000;

    function tick() {
      var left = Math.ceil((end - Date.now()) / 1000);
      if (left > 0) {
        if (text) text.textContent = 'Too many failed attempts. Try again in ' + left + (left === 1 ? ' second.' : ' seconds.');
        window.setTimeout(tick, 1000);
        return;
      }
      form.removeAttribute('data-lockout-seconds');
      if (button) {
        button.disabled = false;
        button.textContent = 'Log in';
      }
      if (banner) {
        banner.className = 'banner banner--info';
        banner.setAttribute('role', 'status');
      }
      if (text) text.textContent = 'You can try again now.';
    }

    tick();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
