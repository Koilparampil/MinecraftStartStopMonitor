/*
 * minecraft-monitor dashboard. Vanilla JS: no dependencies, no build step.
 *
 * - Every server-provided string reaches the DOM through textContent or
 *   setAttribute (never innerHTML).
 * - Snapshots are applied in revision order; older ones are ignored and the
 *   counter resets whenever the live stream (re)connects.
 * - A 401 from any request sends the browser to the login page.
 */
(function () {
  'use strict';

  var CONSOLE_INTERVAL_MS = 2500;
  var INFO_INTERVAL_MS = 60000;
  var AVATAR_BASE = 'https://mc-heads.net/avatar/';
  var SVG_NS = 'http://www.w3.org/2000/svg';

  var $ = function (id) { return document.getElementById(id); };

  function readJson(id) {
    var node = $(id);
    if (!node) return null;
    try {
      return JSON.parse(node.textContent);
    } catch (error) {
      return null;
    }
  }

  var boot = readJson('bootstrap') || {};
  var config = boot.config || {};
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrfToken = csrfMeta ? csrfMeta.getAttribute('content') : '';

  var view = {
    revision: -1,
    snapshot: null,
    clockOffset: 0,
    autoStopDelay: Number(config.autoStopDelay) || 0,
    lastUpdate: null,
    info: null,
    playersKey: '',
    controlBusy: false,
    previousStatus: null,  // status of the last snapshot on this stream
    userStopAt: 0  // when the admin last asked for a stop/restart (ms)
  };

  // ---------------------------------------------------------------------
  // DOM and formatting helpers
  // ---------------------------------------------------------------------

  function h(tag, attrs) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else node.setAttribute(key, value === true ? '' : String(value));
      });
    }
    for (var i = 2; i < arguments.length; i += 1) {
      var child = arguments[i];
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  function icon(name) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'ico');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    var use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#' + name);
    svg.append(use);
    return svg;
  }

  function setText(node, text) {
    if (node && node.textContent !== text) node.textContent = text;
  }

  // Attribute-based so it also works on SVG elements (no .hidden property).
  function setHidden(node, hidden) {
    if (node && node.hasAttribute('hidden') !== hidden) node.toggleAttribute('hidden', hidden);
  }

  function setDisabled(node, disabled) {
    if (node && node.disabled !== disabled) node.disabled = disabled;
  }

  function setBusy(button, busy) {
    if (!button) return;
    button.classList.toggle('is-busy', busy);
    if (busy) {
      button.setAttribute('aria-busy', 'true');
      button.disabled = true;
    } else {
      button.removeAttribute('aria-busy');
    }
  }

  function serverNow() {
    return Date.now() / 1000 + view.clockOffset;
  }

  function stripCodes(text) {
    return String(text === null || text === undefined ? '' : text).replace(/\u00a7./g, '');
  }

  function plural(count, one, many) {
    return count + ' ' + (count === 1 ? one : (many || one + 's'));
  }

  function pad2(value) {
    return String(value).padStart(2, '0');
  }

  // 9:41, 1:02:03
  function formatClock(totalSeconds) {
    var s = Math.max(0, Math.ceil(totalSeconds));
    var hours = Math.floor(s / 3600);
    var minutes = Math.floor((s % 3600) / 60);
    var seconds = s % 60;
    if (hours) return hours + ':' + pad2(minutes) + ':' + pad2(seconds);
    return minutes + ':' + pad2(seconds);
  }

  // 45s, 12m, 3h 05m, 2d 4h
  function formatDuration(totalSeconds) {
    var s = Math.max(0, Math.floor(totalSeconds));
    if (s < 60) return s + 's';
    var minutes = Math.floor(s / 60);
    if (minutes < 60) return minutes + 'm';
    var hours = Math.floor(minutes / 60);
    if (hours < 48) return hours + 'h ' + pad2(minutes % 60) + 'm';
    return Math.floor(hours / 24) + 'd ' + (hours % 24) + 'h';
  }

  // "10 minutes", "1 minute 30 seconds", "45 seconds"
  function formatDelay(totalSeconds) {
    var s = Math.max(0, Math.round(totalSeconds));
    var minutes = Math.floor(s / 60);
    var seconds = s % 60;
    if (!minutes) return plural(seconds, 'second');
    if (!seconds) return plural(minutes, 'minute');
    return plural(minutes, 'minute') + ' ' + plural(seconds, 'second');
  }

  function formatAgo(timestamp) {
    if (!timestamp) return 'Never seen online';
    var diff = serverNow() - timestamp;
    if (diff < 60) return 'Seen just now';
    if (diff < 3600) return 'Seen ' + Math.floor(diff / 60) + ' min ago';
    if (diff < 86400) return 'Seen ' + plural(Math.floor(diff / 3600), 'hour') + ' ago';
    if (diff < 30 * 86400) return 'Seen ' + plural(Math.floor(diff / 86400), 'day') + ' ago';
    return 'Seen ' + new Date(timestamp * 1000).toLocaleDateString();
  }

  function hueOf(text) {
    var hash = 7;
    var value = String(text);
    for (var i = 0; i < value.length; i += 1) {
      hash = (hash * 31 + value.charCodeAt(i)) >>> 0;
    }
    return hash % 360;
  }

  // ---------------------------------------------------------------------
  // Requests
  // ---------------------------------------------------------------------

  function AuthRequired() {
    this.message = 'Session expired';
  }

  function goToLogin() {
    var here = window.location.pathname + window.location.search;
    var query = here && here !== '/' ? '?next=' + encodeURIComponent(here) : '';
    window.location.assign('/login' + query);
  }

  function api(path, fields) {
    var options = {
      method: fields ? 'POST' : 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    };
    if (fields) {
      var body = new URLSearchParams();
      Object.keys(fields).forEach(function (key) { body.append(key, fields[key]); });
      body.append('csrf_token', csrfToken);
      options.body = body;
      options.headers['X-CSRFToken'] = csrfToken;
    }
    return fetch(path, options).then(function (response) {
      if (response.status === 401) {
        goToLogin();
        throw new AuthRequired();
      }
      return response.json().catch(function () { return null; }).then(function (data) {
        if (!data) throw new Error('Unexpected response from the monitor (HTTP ' + response.status + ').');
        return { ok: response.ok, status: response.status, data: data };
      });
    }, function () {
      throw new Error('Could not reach the monitor. Is it running?');
    });
  }

  function reportError(error) {
    if (error instanceof AuthRequired) return;
    toast(error && error.message ? error.message : 'Something went wrong.', 'error');
  }

  // ---------------------------------------------------------------------
  // Toasts
  // ---------------------------------------------------------------------

  var TOAST_ICONS = { success: 'i-check', error: 'i-alert', info: 'i-info' };

  function toast(message, kind, timeout, action) {
    var box = $('toasts');
    if (!box) return;
    kind = kind || 'info';
    var close = h('button', { type: 'button', class: 'icon-btn', 'aria-label': 'Dismiss notification' }, icon('i-close'));
    var label = kind === 'error' ? h('span', { class: 'sr-only', text: 'Error: ' }) : null;
    var actionButton = action ? h('button', { type: 'button', class: 'toast-action', text: action.label }) : null;
    var node = h('div', { class: 'toast toast--' + kind },
      icon(TOAST_ICONS[kind] || 'i-info'),
      h('div', { class: 'toast-body' }, h('p', { class: 'toast-msg' }, label, String(message)), actionButton),
      close);
    var removed = false;
    function remove() {
      if (removed) return;
      removed = true;
      node.classList.add('is-leaving');
      window.setTimeout(function () { node.remove(); }, 170);
    }
    close.addEventListener('click', remove);
    if (actionButton) {
      actionButton.addEventListener('click', function () { remove(); action.run(); });
    }
    box.append(node);
    while (box.childElementCount > 4) box.firstElementChild.remove();
    window.setTimeout(remove, timeout || (kind === 'error' ? 9000 : 5000));
  }

  // ---------------------------------------------------------------------
  // Confirm dialog (in-page <dialog>, never window.confirm)
  // ---------------------------------------------------------------------

  var dialog = $('confirmDialog');
  var TONES = {
    danger: { button: 'btn btn--danger', icon: 'modal-icon' },
    warn: { button: 'btn btn--warn', icon: 'modal-icon modal-icon--warn' },
    primary: { button: 'btn btn--primary', icon: 'modal-icon modal-icon--info' }
  };

  function confirmAction(options) {
    return new Promise(function (resolve) {
      if (!dialog || typeof dialog.showModal !== 'function') {
        toast('This browser cannot show confirmation dialogs.', 'error');
        resolve(null);
        return;
      }
      var tone = TONES[options.tone] || TONES.danger;
      var ok = $('confirmOk');
      var reasonField = $('confirmReasonField');
      var reasonInput = $('confirmReason');
      var iconBox = $('confirmIcon');
      setText($('confirmTitle'), options.title);
      setText($('confirmBody'), options.body);
      setText(ok, options.confirmLabel || 'Confirm');
      setText($('confirmCancel'), options.cancelLabel || 'Cancel');
      ok.className = tone.button;
      iconBox.className = tone.icon;
      iconBox.replaceChildren(icon(options.icon || 'i-alert'));
      reasonField.hidden = !options.reason;
      reasonInput.value = '';

      var opener = document.activeElement;
      var openerKey = opener && opener.dataset ? opener.dataset.focusKey : null;
      function onClose() {
        dialog.removeEventListener('close', onClose);
        var confirmed = dialog.returnValue === 'confirm';
        restoreFocus(opener, openerKey);
        resolve(confirmed ? { reason: reasonInput.value.trim() } : null);
      }
      dialog.addEventListener('close', onClose);
      dialog.returnValue = '';
      dialog.showModal();
      var focusTarget = options.reason ? reasonInput : (options.focusConfirm ? ok : $('confirmCancel'));
      focusTarget.focus();
    });
  }

  function restoreFocus(opener, key) {
    if (opener && document.contains(opener) && !opener.disabled) {
      opener.focus();
      return;
    }
    if (key) {
      var again = document.querySelector('[data-focus-key="' + cssEscape(key) + '"]');
      if (again && !again.disabled) again.focus();
    }
  }

  function cssEscape(value) {
    return window.CSS && window.CSS.escape ? window.CSS.escape(value) : String(value).replace(/["\\]/g, '\\$&');
  }

  function wireDialog() {
    if (!dialog) return;
    $('confirmCancel').addEventListener('click', function () { dialog.close('cancel'); });
    dialog.addEventListener('click', function (event) {
      if (event.target === dialog) dialog.close('cancel');  // backdrop click
    });
  }

  // ---------------------------------------------------------------------
  // Snapshots
  // ---------------------------------------------------------------------

  function applyPayload(payload) {
    if (!payload || typeof payload !== 'object' || !payload.data) return false;
    if (typeof payload.serverTime === 'number') {
      view.clockOffset = payload.serverTime - Date.now() / 1000;
    }
    if (typeof payload.autoStopDelay === 'number') {
      view.autoStopDelay = payload.autoStopDelay;
    }
    return applySnapshot(payload.data);
  }

  function applySnapshot(snapshot) {
    var revision = typeof snapshot.revision === 'number' ? snapshot.revision : 0;
    if (revision < view.revision) return false;  // stale: a newer one is shown
    view.revision = revision;
    view.snapshot = snapshot;
    view.lastUpdate = new Date();
    noticeFailedLaunch(snapshot);
    render();
    return true;
  }

  // STARTING/LOADING -> STOPPED that nobody asked for: the launch failed or
  // the server crashed while loading. Point the admin at the launch output.
  function noticeFailedLaunch(snapshot) {
    var status = statusOf(snapshot);
    var before = view.previousStatus;
    view.previousStatus = status;
    if (status !== 'OFFLINE' || (before !== 'STARTING' && before !== 'LOADING')) return;
    if (Date.now() - view.userStopAt < 120000) return;  // the admin stopped it
    if (!$('consolePanel')) {
      toast('The server stopped while starting. Check the server logs.', 'error', 15000);
      return;
    }
    toast('The server stopped while starting — see Launch output', 'error', 20000,
      { label: 'Show launch output', run: showLaunchOutput });
  }

  function showLaunchOutput() {
    var radio = document.querySelector('input[name="consoleSource"][value="launch"]');
    var details = $('consoleDetails');
    if (details && !details.open) details.open = true;
    if (radio && !radio.checked) {
      radio.checked = true;
      radio.dispatchEvent(new Event('change'));
    }
    $('consolePanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (radio) radio.focus({ preventScroll: true });
  }

  function statusOf(snapshot) {
    if (!snapshot) return 'OFFLINE';
    if (snapshot.state === 'RUNNING') return snapshot.ready ? 'ONLINE' : 'LOADING';
    // The instant between a restart's stop and its start is not "offline".
    if (snapshot.state === 'STOPPED' && snapshot.restartPending) return 'STARTING';
    if (snapshot.state === 'STARTING' || snapshot.state === 'STOPPING') return snapshot.state;
    return 'OFFLINE';
  }

  function render() {
    var snapshot = view.snapshot;
    if (!snapshot) return;
    renderStatus(snapshot);
    renderControls(snapshot);
    renderAutomation(snapshot);
    renderCountdown();
    renderPlayers(snapshot);
    renderFacts(snapshot);
    renderCommandAvailability(snapshot);
    renderUptime();
    renderLastUpdated();
  }

  // ---------------------------------------------------------------------
  // Header
  // ---------------------------------------------------------------------

  function statusDetail(snapshot, status) {
    switch (status) {
      case 'STARTING': return snapshot.restartPending ? 'Restarting — starting again…' : 'Launching the server…';
      case 'LOADING': return 'Process is up — loading mods…';
      case 'ONLINE': return 'Accepting players';
      case 'STOPPING':
        return snapshot.restartPending ? 'Restarting — saving worlds first…' : 'Saving worlds and shutting down…';
      default:
        return config.autoStart ? 'Sleeping — a join attempt wakes it' : 'Stopped';
    }
  }

  function renderStatus(snapshot) {
    var status = statusOf(snapshot);
    var pill = $('statusPill');
    var pillClass = 'pill pill--' + status.toLowerCase();
    if (pill && pill.className !== pillClass) pill.className = pillClass;
    setText($('statusText'), status);
    setText($('statusDetail'), statusDetail(snapshot, status));

    var server = snapshot.server || {};
    var motd = stripCodes(server.motd).trim();
    setText($('serverMotd'), motd || 'Minecraft server');
    setText($('serverVersion'), server.version ? 'Minecraft ' + server.version : 'Version unknown');
    var running = snapshot.state === 'RUNNING';
    var count = running ? (snapshot.playerCount || 0) : 0;
    var summary = snapshot.maxPlayers ? count + ' / ' + snapshot.maxPlayers + ' players' : plural(count, 'player');
    setText($('playerSummary'), summary);
    document.title = status + ' · ' + (motd.split('\n')[0] || 'Minecraft server') + ' · Minecraft Monitor';
  }

  function renderUptime() {
    var snapshot = view.snapshot;
    var node = $('uptime');
    if (!snapshot || !node) return;
    var show = snapshot.state === 'RUNNING' && typeof snapshot.startedAt === 'number';
    setHidden(node, !show);
    if (show) setText(node, 'Up ' + formatDuration(serverNow() - snapshot.startedAt));
  }

  // ---------------------------------------------------------------------
  // Controls
  // ---------------------------------------------------------------------

  function controlsHint(snapshot, status) {
    switch (status) {
      case 'STARTING':
        return snapshot.restartPending ? 'Restarting: the server is starting again.' : 'The server is starting. Controls unlock once the process is up.';
      case 'LOADING': return 'Mods are loading. You can still stop or restart if it hangs.';
      case 'STOPPING':
        return snapshot.restartPending ? 'Restarting: it starts again as soon as it has stopped. "Cancel restart" keeps it offline.' : 'Stopping: the world is being saved.';
      case 'ONLINE': return 'Stop and restart save the world first.';
      default:
        return config.autoStart ? 'Start it now, or let the first player who joins wake it up.' : 'Start the server when you need it.';
    }
  }

  function cancelsRestart(snapshot) {
    return snapshot.state === 'STOPPING' && !!snapshot.restartPending;
  }

  function renderControls(snapshot) {
    if (!config.controlServer) return;
    var state = snapshot.state;
    var busy = view.controlBusy;
    var cancel = cancelsRestart(snapshot);
    setDisabled($('startBtn'), busy || state !== 'STOPPED' || !!snapshot.restartPending);
    setDisabled($('stopBtn'), busy || !(state === 'RUNNING' || cancel));
    setText($('stopLabel'), cancel ? 'Cancel restart' : 'Stop');
    setDisabled($('restartBtn'), busy || state !== 'RUNNING');
    setText($('controlsHint'), controlsHint(snapshot, statusOf(snapshot)));
  }

  var CONTROL_COPY = {
    stopServer: {
      title: 'Stop the server?',
      body: 'The world is saved before the server shuts down.',
      confirmLabel: 'Stop server',
      tone: 'danger',
      icon: 'i-stop'
    },
    restartServer: {
      title: 'Restart the server?',
      body: 'The world is saved, the server stops and then starts again.',
      confirmLabel: 'Restart',
      tone: 'warn',
      icon: 'i-restart'
    }
  };

  var CANCEL_RESTART_COPY = {
    title: 'Cancel the restart?',
    body: 'The server finishes shutting down and stays offline instead of starting again.',
    confirmLabel: 'Cancel restart',
    cancelLabel: 'Keep restarting',
    tone: 'warn',
    icon: 'i-stop'
  };

  function runControl(action, button) {
    var copy = CONTROL_COPY[action];
    if (action === 'stopServer' && view.snapshot && cancelsRestart(view.snapshot)) copy = CANCEL_RESTART_COPY;
    var confirmation = Promise.resolve({});
    if (copy === CANCEL_RESTART_COPY) {
      confirmation = confirmAction(copy);
    } else if (copy) {
      var count = view.snapshot && view.snapshot.state === 'RUNNING' ? (view.snapshot.playerCount || 0) : 0;
      var who = count ? ' ' + plural(count, 'player') + ' online will be disconnected.' : ' Nobody is online.';
      confirmation = confirmAction(Object.assign({}, copy, { body: copy.body + who }));
    }
    return confirmation.then(function (answer) {
      if (!answer) return null;
      view.controlBusy = true;
      setBusy(button, true);
      renderControls(view.snapshot || {});
      return api('/action', { action: action }).then(function (result) {
        if (result.data.success && action !== 'startServer') view.userStopAt = Date.now();
        applyPayload(result.data);
        toast(result.data.message || (result.data.success ? 'Done.' : 'Request failed.'),
          result.data.success ? 'success' : 'error');
      }).catch(reportError).then(function () {
        view.controlBusy = false;
        setBusy(button, false);
        render();
      });
    });
  }

  function refreshAll(button) {
    setBusy(button, true);
    var status = api('/action', { action: 'getStatus' }).then(function (result) {
      applyPayload(result.data);
    });
    return Promise.all([status, loadInfo(), refreshConsole(true)]).catch(reportError).then(function () {
      setBusy(button, false);
      button.disabled = false;
    });
  }

  function wireControls() {
    [['startBtn', 'startServer'], ['stopBtn', 'stopServer'], ['restartBtn', 'restartServer']].forEach(function (pair) {
      var button = $(pair[0]);
      if (button) button.addEventListener('click', function () { runControl(pair[1], button); });
    });
    var refresh = $('refreshBtn');
    if (refresh) refresh.addEventListener('click', function () { refreshAll(refresh); });
  }

  // ---------------------------------------------------------------------
  // Automation panel and auto-stop countdown
  // ---------------------------------------------------------------------

  function renderAutomation(snapshot) {
    var startText = $('autoStartText');
    if (startText && config.autoStart) {
      var motd = view.info && view.info.sleepingMotd;
      if (snapshot.state === 'STOPPED' && !snapshot.restartPending) {
        var parts = ['Sleeping: the monitor answers on the game port'];
        if (motd) {
          startText.replaceChildren(parts[0] + '. Players see ', h('q', { text: motd }),
            ' in their server list; the first join attempt wakes the server.');
        } else {
          startText.replaceChildren(parts[0] + '; the first player who tries to join wakes the server.');
        }
      } else {
        setText(startText, 'The server is awake. After it stops, the monitor listens on the game port again and wakes it on the next join.');
      }
    }
    var stopText = $('autoStopText');
    if (stopText && config.autoStop) {
      setText(stopText, 'Stops the server gracefully after ' + formatDelay(view.autoStopDelay) + ' with no players online.');
    }
  }

  function setCountdown(className, parts, fraction) {
    var box = $('autoStopCountdown');
    var text = $('autoStopCountdownText');
    if (box.className !== className) box.className = className;
    text.replaceChildren.apply(text, parts);
    $('autoStopMeter').style.width = Math.round(Math.max(0, Math.min(1, fraction)) * 1000) / 10 + '%';
  }

  function renderCountdown() {
    var snapshot = view.snapshot;
    var box = $('autoStopCountdown');
    if (!box || !snapshot) return;
    var show = config.autoStop && snapshot.state === 'RUNNING';
    setHidden(box, !show);
    if (!show) return;
    var delay = view.autoStopDelay;
    var count = snapshot.playerCount || 0;
    if (snapshot.playerData === 'unavailable') {
      // Auto-stop only trusts fresh player counts: it waits while the server is silent.
      setCountdown('countdown', ['Paused — the server is not answering'], 1);
    } else if (typeof snapshot.autoStopAt === 'number') {
      var remaining = snapshot.autoStopAt - serverNow();
      if (remaining <= 0) {
        setCountdown('countdown countdown--armed', ['Stopping now — no players online'], 0);
      } else {
        setCountdown('countdown countdown--armed', ['Stopping in ', h('span', { class: 'countdown-time', text: formatClock(remaining) }), ' — no players online'],
          delay > 0 ? remaining / delay : 0);
      }
    } else if (!snapshot.ready) {
      setCountdown('countdown', ['Waits until the server has finished loading'], 1);
    } else if (count > 0) {
      setCountdown('countdown countdown--paused', [plural(count, 'player') + ' online — timer paused'], 1);
    } else {
      setCountdown('countdown', ['No players online — the countdown starts shortly'], 1);
    }
  }

  // ---------------------------------------------------------------------
  // Players
  // ---------------------------------------------------------------------

  function playerRows(snapshot) {
    var banned = new Map();
    (snapshot.banned_players || []).forEach(function (entry) {
      if (entry && entry.name) banned.set(String(entry.name).toLowerCase(), entry);
    });
    var rows = [];
    var seen = new Set();
    (snapshot.players || []).forEach(function (player) {
      if (!player || !player.name) return;
      var key = String(player.name).toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);
      rows.push({
        key: key,
        name: String(player.name),
        uuid: player.uuid || null,
        online: !!player.currently_online,
        lastOnline: player.last_online || null,
        ban: banned.get(key) || null
      });
    });
    banned.forEach(function (entry, key) {
      if (!seen.has(key)) {
        rows.push({ key: key, name: String(entry.name), uuid: entry.uuid || null, online: false, lastOnline: null, ban: entry });
      }
    });
    rows.sort(function (a, b) {
      return (b.online - a.online) || (!!a.ban - !!b.ban) ||
        ((b.lastOnline || 0) - (a.lastOnline || 0)) || a.name.localeCompare(b.name);
    });
    return rows;
  }

  function moderationState(snapshot) {
    var viaPing = !!(snapshot.server && snapshot.server.source === 'ping');
    var online = snapshot.state === 'RUNNING' && snapshot.ready;
    if (snapshot.state === 'STOPPED' && !snapshot.restartPending) return { banOk: true, kickOk: false, why: 'Kicking needs a running server' };
    if (!online) return { banOk: false, kickOk: false, why: 'Wait until the server has started or stopped' };
    if (viaPing) return { banOk: false, kickOk: false, why: 'Needs RCON (enable-rcon in server.properties)' };
    return { banOk: true, kickOk: true, why: '' };
  }

  function actionButton(kind, row, enabled, why) {
    var labels = { kick: 'Kick', ban: 'Ban', unban: 'Unban' };
    var tones = { kick: 'tone-warn', ban: 'tone-danger', unban: 'tone-ok' };
    return h('button', {
      type: 'button',
      class: 'btn btn--xs btn--ghost ' + tones[kind],
      'data-player-action': kind,
      'data-player-name': row.name,
      'data-focus-key': kind + ':' + row.key,
      'aria-label': labels[kind] + ' ' + row.name,
      title: enabled ? labels[kind] + ' ' + row.name : why,
      disabled: !enabled
    }, icon('i-' + kind), h('span', { text: labels[kind] }));
  }

  function rowActions(row, snapshot) {
    var rules = moderationState(snapshot);
    var box = h('div', { class: 'row-actions' });
    if (row.online) box.append(actionButton('kick', row, rules.kickOk, rules.why));
    box.append(row.ban ? actionButton('unban', row, rules.banOk, rules.why) : actionButton('ban', row, rules.banOk, rules.why));
    return box;
  }

  function avatarId(row) {
    // Offline-mode (version 3) UUIDs are unknown to skin services: use the name.
    if (row.uuid && /^[0-9a-f]{8}-?[0-9a-f]{4}-?4/i.test(row.uuid)) return row.uuid.replace(/-/g, '');
    return row.name;
  }

  function initialAvatar(row) {
    var letter = (row.name.match(/[A-Za-z0-9]/) || ['?'])[0].toUpperCase();
    var node = h('span', { class: 'avatar avatar--initial', 'aria-hidden': 'true', text: letter });
    node.style.setProperty('--hue', String(hueOf(row.key)));
    return node;
  }

  function avatar(row) {
    if (!config.playerAvatars) return initialAvatar(row);
    var id = encodeURIComponent(avatarId(row));
    var img = h('img', {
      class: 'avatar',
      src: AVATAR_BASE + id + '/32',
      srcset: AVATAR_BASE + id + '/64 2x',
      width: 32,
      height: 32,
      alt: '',
      loading: 'lazy',
      decoding: 'async',
      referrerpolicy: 'no-referrer'
    });
    img.addEventListener('error', function () { img.replaceWith(initialAvatar(row)); }, { once: true });
    return img;
  }

  function statusCell(row) {
    if (row.ban) {
      var details = [];
      if (row.ban.expires && row.ban.expires !== 'forever') details.push('until ' + row.ban.expires);
      if (row.ban.source) details.push('by ' + row.ban.source);
      return h('span', { class: 'player-status-wrap', title: details.join(' ') || null },
        h('span', { class: 'player-status player-status--banned', text: row.online ? 'Banned · online' : 'Banned' }),
        row.ban.reason ? h('span', { class: 'ban-reason', text: row.ban.reason }) : null);
    }
    if (row.online) return h('span', { class: 'player-status player-status--online', text: 'Online' });
    return h('span', {
      class: 'player-status player-status--offline player-seen',
      'data-ts': row.lastOnline || '',
      title: row.lastOnline ? new Date(row.lastOnline * 1000).toLocaleString() : null,
      text: formatAgo(row.lastOnline)
    });
  }

  function playerRow(row, snapshot) {
    var names = h('div', { class: 'player-names' }, h('span', { class: 'player-name', text: row.name }),
      row.uuid ? h('span', { class: 'player-uuid', text: row.uuid, title: row.uuid }) : null);
    return h('tr', { class: row.ban ? 'is-banned' : (row.online ? 'is-online' : 'is-offline'), 'data-player': row.key },
      h('td', { class: 'cell-player' }, h('div', { class: 'player-cell' }, avatar(row), names)),
      h('td', { class: 'cell-status' }, statusCell(row)),
      h('td', { class: 'cell-actions' }, rowActions(row, snapshot)));
  }

  function notice(text, kind, title) {
    return h('div', { class: 'notice' + (kind ? ' notice--' + kind : ''), title: title || null },
      icon(kind === 'info' ? 'i-info' : 'i-alert'), h('span', { text: text }));
  }

  function renderPlayerMeta(snapshot, rows) {
    var running = snapshot.state === 'RUNNING';
    var count = running ? (snapshot.playerCount || 0) : 0;
    var known = rows.filter(function (row) { return row.online; }).length;
    setText($('playerCountChip'), running ? count + ' online' : 'Server offline');

    var notices = [];
    if (running && snapshot.playerData === 'unavailable') {
      notices.push(notice('The server is not answering. Showing the last known roster.'));
    } else if (running && snapshot.playerData === 'unknown') {
      notices.push(notice('Waiting for the first player list…', 'info'));
    }
    if (running && count > known) {
      notices.push(notice((count - known) + ' online, names hidden', 'info',
        'The server reports more players than it names (status-ping samples are capped, or hide-online-players is on).'));
    }
    var box = $('playersNotices');
    var noticeKey = notices.map(function (node) { return node.textContent; }).join('|');
    if (box.dataset.key !== noticeKey) {
      box.dataset.key = noticeKey;
      box.replaceChildren.apply(box, notices);
    }

    setHidden($('playersTable'), rows.length === 0);
    setHidden($('playersEmpty'), rows.length !== 0);
    if (!rows.length) {
      setText($('playersEmptyText'), running || snapshot.state === 'STOPPED'
        ? 'No players yet. Everyone who joins shows up here, with last-seen times and moderation tools.'
        : 'Players appear here once the server is up.');
    }
  }

  function renderPlayers(snapshot) {
    var rows = playerRows(snapshot);
    renderPlayerMeta(snapshot, rows);
    var key = JSON.stringify([snapshot.state, !!snapshot.ready, snapshot.server && snapshot.server.source,
      config.playerAvatars, rows.map(function (row) {
        return [row.key, row.name, row.uuid, row.online, row.online ? 0 : row.lastOnline,
          row.ban ? [row.ban.reason, row.ban.expires, row.ban.source] : 0];
      })]);
    if (key === view.playersKey) return;
    view.playersKey = key;
    var active = document.activeElement;
    var focusKey = active && active.dataset ? active.dataset.focusKey : null;
    var body = $('playersBody');
    body.replaceChildren.apply(body, rows.map(function (row) { return playerRow(row, snapshot); }));
    if (focusKey && body.contains(active) === false) {
      var target = body.querySelector('[data-focus-key="' + cssEscape(focusKey) + '"]');
      if (target && !target.disabled) target.focus();
    }
  }

  function refreshSeen() {
    document.querySelectorAll('.player-seen[data-ts]').forEach(function (node) {
      var ts = Number(node.getAttribute('data-ts'));
      setText(node, formatAgo(ts || null));
    });
  }

  var PLAYER_COPY = {
    kick: function (name) {
      return { title: 'Kick ' + name + '?', body: name + ' is disconnected and can rejoin right away.',
        confirmLabel: 'Kick', tone: 'warn', icon: 'i-kick', reason: true };
    },
    ban: function (name, snapshot) {
      var body = snapshot.state === 'RUNNING'
        ? name + ' is disconnected and cannot rejoin until unbanned.'
        : name + ' is added to the server ban list and cannot join until unbanned.';
      return { title: 'Ban ' + name + '?', body: body, confirmLabel: 'Ban', tone: 'danger', icon: 'i-ban', reason: true };
    },
    unban: function (name) {
      return { title: 'Unban ' + name + '?', body: name + ' will be able to join again.',
        confirmLabel: 'Unban', tone: 'primary', icon: 'i-unban', reason: false, focusConfirm: true };
    }
  };

  function runPlayerAction(kind, name, button) {
    var copy = PLAYER_COPY[kind](name, view.snapshot || {});
    return confirmAction(copy).then(function (answer) {
      if (!answer) return null;
      var fields = { player: name };
      if (copy.reason && answer.reason) fields.reason = answer.reason;
      if (button && document.contains(button)) setBusy(button, true);
      return api('/' + kind, fields).then(function (result) {
        if (result.data.data) applyPayload(result.data);
        toast(result.data.message || (result.data.success ? 'Done.' : 'Request failed.'),
          result.data.success ? 'success' : 'error');
      }).catch(reportError).then(function () {
        view.playersKey = '';  // re-render buttons (drops any busy state)
        if (view.snapshot) renderPlayers(view.snapshot);
      });
    });
  }

  function wirePlayers() {
    $('playersBody').addEventListener('click', function (event) {
      var button = event.target.closest('button[data-player-action]');
      if (!button || button.disabled) return;
      runPlayerAction(button.getAttribute('data-player-action'), button.getAttribute('data-player-name'), button);
    });
  }

  // ---------------------------------------------------------------------
  // Server facts and info (icon, mods, address)
  // ---------------------------------------------------------------------

  var SOURCES = { rcon: 'RCON', ping: 'Status ping (no RCON)' };

  function renderFacts(snapshot) {
    var server = snapshot.server || {};
    setText($('factVersion'), server.version || '—');
    setText($('factSlots'), snapshot.maxPlayers ? String(snapshot.maxPlayers) : '—');
    setText($('factSource'), snapshot.state === 'RUNNING' ? (SOURCES[server.source] || 'Waiting…') : '—');
    var started = snapshot.state === 'RUNNING' && typeof snapshot.startedAt === 'number';
    setText($('factStarted'), started ? new Date(snapshot.startedAt * 1000).toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    }) : '—');
  }

  function loadInfo() {
    return api('/info').then(function (result) {
      if (!result.ok) return;
      view.info = result.data;
      renderInfo();
    }).catch(function (error) {
      if (!(error instanceof AuthRequired)) setText($('modsEmpty'), 'Could not load server details.');
    });
  }

  function renderInfo() {
    var info = view.info;
    if (!info) return;
    var img = $('serverIcon');
    var fallback = $('serverIconFallback');
    var hasIcon = typeof info.icon === 'string' && info.icon.indexOf('data:image/') === 0;
    if (hasIcon && img.getAttribute('src') !== info.icon) img.setAttribute('src', info.icon);
    setHidden(img, !hasIcon);
    setHidden(fallback, hasIcon);

    setHidden($('addressFact'), !info.serverAddress);
    if (info.serverAddress) setText($('serverAddress'), info.serverAddress);
    renderMods(Array.isArray(info.mods) ? info.mods : []);
    if (view.snapshot) renderAutomation(view.snapshot);
  }

  function splitModName(file) {
    var base = String(file).replace(/\.jar$/i, '');
    var match = /^(.+?)[-_+ ]v?(\d[\w.+-]*)$/.exec(base);
    return match ? { name: match[1], version: match[2] } : { name: base, version: '' };
  }

  function renderMods(mods) {
    var list = $('modList');
    var key = mods.join('\n');
    setText($('modCount'), mods.length ? plural(mods.length, 'mod') : 'None');
    setHidden($('modFilterWrap'), mods.length < 8);
    if (list.dataset.key !== key) {
      list.dataset.key = key;
      list.replaceChildren.apply(list, mods.map(function (file) {
        var parts = splitModName(file);
        var item = h('li', { title: String(file), 'data-filter': String(file).toLowerCase() },
          h('span', { class: 'mod-name', text: parts.name }),
          parts.version ? h('span', { class: 'mod-version', text: parts.version }) : null);
        item.style.setProperty('--hue', String(hueOf(parts.name.toLowerCase())));
        return item;
      }));
    }
    applyModFilter();
  }

  function applyModFilter() {
    var query = ($('modFilter').value || '').trim().toLowerCase();
    var items = $('modList').children;
    var shown = 0;
    for (var i = 0; i < items.length; i += 1) {
      var match = !query || items[i].getAttribute('data-filter').indexOf(query) !== -1;
      setHidden(items[i], !match);
      if (match) shown += 1;
    }
    var empty = $('modsEmpty');
    var message = '';
    if (!items.length) message = 'No mods found in mods/. This looks like a vanilla server.';
    else if (!shown) message = 'No mods match the filter.';
    setHidden(empty, !message);
    setText(empty, message);
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var area = h('textarea', { class: 'sr-only', readonly: true });
      area.value = text;
      document.body.append(area);
      area.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (error) { ok = false; }
      area.remove();
      if (ok) resolve(); else reject(new Error('Copy failed'));
    });
  }

  function wireInfo() {
    $('modFilter').addEventListener('input', applyModFilter);
    $('copyAddress').addEventListener('click', function () {
      copyText($('serverAddress').textContent).then(function () {
        toast('Address copied.', 'success', 2500);
      }, function () {
        toast('Could not copy the address.', 'error');
      });
    });
  }

  // ---------------------------------------------------------------------
  // Console
  // ---------------------------------------------------------------------

  var consoleState = { source: 'log', timer: null, inflight: null, key: '', history: [], cursor: -1 };
  var META_RE = /^((?:\[[^\]\n]{1,120}\]\s*){1,4}:?\s*)/;

  function consoleOpen() {
    var details = $('consoleDetails');
    return !!details && details.open;
  }

  function scheduleConsole(delay) {
    window.clearTimeout(consoleState.timer);
    consoleState.timer = window.setTimeout(function () { refreshConsole(false); }, delay);
  }

  function refreshConsole(force) {
    if (!$('consoleLog') || !consoleOpen() || (document.hidden && !force)) return Promise.resolve();
    if (consoleState.inflight) return consoleState.inflight;
    var source = consoleState.source;
    var url = '/console?source=' + encodeURIComponent(source) + '&lines=' + encodeURIComponent($('consoleLines').value);
    consoleState.inflight = api(url).then(function (result) {
      if (source !== consoleState.source) return;
      if (result.ok && result.data.success) renderLog(result.data);
      else renderLogMessage(result.data.message || 'The console is unavailable.');
    }).catch(function (error) {
      if (!(error instanceof AuthRequired)) renderLogMessage(error.message);
    }).then(function () {
      consoleState.inflight = null;
      if (consoleOpen() && !document.hidden) scheduleConsole(CONSOLE_INTERVAL_MS);
    });
    return consoleState.inflight;
  }

  function lineClass(text) {
    if (/\/(ERROR|FATAL|SEVERE)\]|^\s+at |^Caused by:|^\S+Exception\b/.test(text)) return 'ln--error';
    if (/\/WARN(ING)?\]/.test(text)) return 'ln--warn';
    if (/Done \(\d|RCON running on|Server started/.test(text)) return 'ln--good';
    return '';
  }

  function logLine(text) {
    var line = h('span', { class: 'ln' });
    var extra = lineClass(text);
    if (extra) line.classList.add(extra);
    var meta = META_RE.exec(text);
    if (meta) line.append(h('span', { class: 'ln-meta', text: meta[1] }), text.slice(meta[1].length));
    else line.textContent = text;
    return line;
  }

  function renderLogMessage(message) {
    consoleState.key = '';
    $('consoleLog').replaceChildren(h('p', { class: 'console-empty', text: message }));
  }

  function renderLog(data) {
    var box = $('consoleLog');
    var lines = Array.isArray(data.lines) ? data.lines : [];
    var key = data.source + '|' + lines.length + '|' + lines[0] + '|' + lines[lines.length - 1];
    if (key === consoleState.key) return;
    consoleState.key = key;
    if (!lines.length) {
      var empty = data.source === 'launch'
        ? 'No launch output yet. It appears once the monitor starts the server.'
        : 'No server log yet (logs/latest.log).';
      box.replaceChildren(h('p', { class: 'console-empty', text: data.exists ? 'The log is empty.' : empty }));
      return;
    }
    var fragment = document.createDocumentFragment();
    lines.forEach(function (line) { fragment.append(logLine(String(line))); });
    box.replaceChildren(fragment);
    if ($('consoleFollow').checked) box.scrollTop = box.scrollHeight;
  }

  function renderCommandAvailability(snapshot) {
    var input = $('commandInput');
    if (!input) return;
    var ready = snapshot.state === 'RUNNING' && !!snapshot.ready;
    setDisabled(input, !ready);
    setDisabled($('commandSend'), !ready);
    var placeholder = ready ? 'list, say Hello, time set day…' : 'Commands are available while the server is online';
    if (input.placeholder !== placeholder) input.placeholder = placeholder;
  }

  function appendTranscript(command, data) {
    var box = $('consoleTranscript');
    var output = data.output !== null && data.output !== undefined && data.output !== ''
      ? stripCodes(data.output)
      : (data.message || (data.success ? '(no output)' : 'Command failed.'));
    box.hidden = false;
    box.append(h('span', { class: 'tx-cmd', text: '> ' + command }),
      h('span', { class: 'tx-out' + (data.success ? '' : ' tx-out--error'), text: output }));
    while (box.childElementCount > 80) box.firstElementChild.remove();
    box.scrollTop = box.scrollHeight;
  }

  function sendCommand(event) {
    event.preventDefault();
    var input = $('commandInput');
    var command = input.value.trim();
    if (!command) return;
    if (consoleState.history[consoleState.history.length - 1] !== command) consoleState.history.push(command);
    if (consoleState.history.length > 50) consoleState.history.shift();
    consoleState.cursor = -1;
    input.value = '';
    var button = $('commandSend');
    setBusy(button, true);
    api('/command', { command: command }).then(function (result) {
      appendTranscript(command.replace(/^\//, ''), result.data);
      if (!result.data.success) toast(result.data.message || 'Command failed.', 'error');
    }).catch(reportError).then(function () {
      setBusy(button, false);
      if (view.snapshot) renderCommandAvailability(view.snapshot);
      input.focus();
      scheduleConsole(500);
    });
  }

  function browseHistory(event) {
    var history = consoleState.history;
    if (!history.length || (event.key !== 'ArrowUp' && event.key !== 'ArrowDown')) return;
    event.preventDefault();
    if (event.key === 'ArrowUp') {
      consoleState.cursor = consoleState.cursor === -1 ? history.length - 1 : Math.max(0, consoleState.cursor - 1);
    } else if (consoleState.cursor !== -1) {
      consoleState.cursor = consoleState.cursor + 1 >= history.length ? -1 : consoleState.cursor + 1;
    }
    var input = event.target;
    input.value = consoleState.cursor === -1 ? '' : history[consoleState.cursor];
    input.setSelectionRange(input.value.length, input.value.length);
  }

  function remember(key, value) {
    try { window.localStorage.setItem(key, value); } catch (error) { /* storage unavailable */ }
  }

  function recall(key) {
    try { return window.localStorage.getItem(key); } catch (error) { return null; }
  }

  function wireConsole() {
    var details = $('consoleDetails');
    if (!details) return;
    if (recall('mcmonitor.consoleOpen') === '0') details.open = false;
    details.addEventListener('toggle', function () {
      remember('mcmonitor.consoleOpen', details.open ? '1' : '0');
      if (details.open) refreshConsole(true);
    });
    document.querySelectorAll('input[name="consoleSource"]').forEach(function (radio) {
      radio.addEventListener('change', function () {
        if (!radio.checked) return;
        consoleState.source = radio.value;
        setText($('consoleChip'), radio.value === 'launch' ? 'Launch output' : 'Server log');
        renderLogMessage('Loading…');
        refreshConsole(true);
      });
    });
    $('consoleLines').addEventListener('change', function () { consoleState.key = ''; refreshConsole(true); });
    var log = $('consoleLog');
    log.addEventListener('scroll', function () {
      var atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
      var follow = $('consoleFollow');
      if (follow.checked !== atBottom) follow.checked = atBottom;
    });
    $('consoleFollow').addEventListener('change', function (event) {
      if (event.target.checked) log.scrollTop = log.scrollHeight;
    });
    var form = $('commandForm');
    if (form) {
      form.addEventListener('submit', sendCommand);
      $('commandInput').addEventListener('keydown', browseHistory);
    }
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) refreshConsole(false);
    });
  }

  // ---------------------------------------------------------------------
  // Live updates (Server-Sent Events) and footer
  // ---------------------------------------------------------------------

  var stream = null;
  var retryDelay = 2000;
  var retryTimer = null;

  function setConnection(kind, text) {
    var node = $('connState');
    var className = 'conn conn--' + kind;
    if (node.className !== className) node.className = className;
    setText($('connText'), text);
  }

  function connectStream() {
    window.clearTimeout(retryTimer);
    if (stream) stream.close();
    setConnection('connecting', 'Connecting…');
    var source = new EventSource('/stream');
    stream = source;
    source.onopen = function () {
      // A reconnected stream may come from a restarted monitor whose
      // revisions start over: accept its first snapshot whatever it is.
      view.revision = -1;
      view.previousStatus = null;
      retryDelay = 2000;
      setConnection('live', 'Live');
    };
    source.onmessage = function (event) {
      try {
        applyPayload(JSON.parse(event.data));
      } catch (error) {
        window.console.error('Bad stream message', error);
      }
    };
    source.onerror = function () {
      if (source.readyState !== EventSource.CLOSED) {
        setConnection('connecting', 'Reconnecting…');
        return;
      }
      // The server refused the stream (expired session, restart, overload):
      // a 401 on this probe redirects to the login page.
      setConnection('offline', 'Disconnected — retrying');
      api('/info').catch(function () {});
      retryTimer = window.setTimeout(connectStream, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 30000);
    };
  }

  function renderLastUpdated() {
    var node = $('lastUpdated');
    if (!node || !view.lastUpdate) return;
    setText(node, view.lastUpdate.toLocaleTimeString());
    node.setAttribute('datetime', view.lastUpdate.toISOString());
  }

  // ---------------------------------------------------------------------
  // Start
  // ---------------------------------------------------------------------

  function init() {
    wireDialog();
    wireControls();
    wirePlayers();
    wireInfo();
    wireConsole();
    if (boot.payload) applyPayload(boot.payload);
    connectStream();
    loadInfo();
    refreshConsole(true);
    window.setInterval(function () { renderCountdown(); renderUptime(); }, 1000);
    window.setInterval(refreshSeen, 30000);
    window.setInterval(loadInfo, INFO_INTERVAL_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
