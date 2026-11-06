# Follow-up plan

Open items from the final verification round, with a plan to close each one.

**Status: all eight items are done.** Each item below ends with a
**Done** note: what changed, where it differs from the proposed fix, and the
tests that cover it.

## Where things stand

- **QA history.** An independent QA pass probed the running app: auto-start,
  auto-stop, the dashboard in a real browser, security, setup and docs, and
  robustness. It confirmed **47 issues**. Three fix rounds followed; fresh
  agents re-ran every reproduction after each round, and all 47 are closed.
- **Final verification.** It checked the round-3 changes on the tree now on
  `main` (`710a1fe`, identical to the verified `90bdde9`). Of the 11 items
  reopened in round 2, **6 are fixed** and **5 are partially fixed**. The
  fixed items include the one critical issue: an unauthenticated
  "log out everywhere".
- **New findings.** The final round found **7 new minor regressions**.
- **Nothing critical or major was open.** Every remaining item was a minor
  edge case: deliberate connection floods aimed at the sleeping listener,
  unusual launch-script layouts, a malformed proxy header, or a short UI
  glitch.
- **Tests (before this plan).** `uv run poe test` gave 1275 passed and 1
  skipped (a Windows-only test). `uv run poe smoke` gave 6 passed. Lint was
  clean.
- **Tests (after this plan).** `uv run poe test` gives 1352 passed and 1
  skipped (the same Windows-only test). `uv run poe smoke` gives 7 passed.
  Lint is clean.
- **Live check.** The listener scenarios were re-run against a live monitor
  (`python -m src.main` with the dummy server):
  - 4 held legacy pings, then 8 idle reconnecting lanes, from the player's
    address: the join woke the server on the first attempt each time;
  - 12 × 30 refused joins: 360 of 360 got a message, and none woke it;
  - forged names were logged escaped, on one line.
  FU-7 was checked the same way: SIGINT during a Restart's wait for
  `run.sh` ended the monitor in 0.1 s. No server was launched, and `run.sh`
  was left running and saved for the next start.

Severity scale used below (the same as in QA):
- **critical:** data loss, auto-stop with players online, a server that can
  never be woken, or a security hole.
- **major:** a core feature broken in a realistic scenario.
- **minor:** everything else.

## Order of work

| # | Item | Area | Severity | Effort | Status |
|---|------|------|----------|--------|--------|
| 1 | [FU-1](#fu-1-x-forwarded-port-with-non-ascii-digits-crashes-requests) X-Forwarded-Port with non-ASCII digits crashes requests | web | minor (anonymous log flooding) | S | Done |
| 2 | [FU-2](#fu-2-invalid-player-names-are-logged-verbatim) Invalid player names logged verbatim | auto-start | minor (log forging) | S | Done |
| 3 | [FU-3](#fu-3-legacy-pings-are-never-evicted) Legacy pings are never evicted | auto-start | minor (wake can be blocked) | S | Done |
| 4 | [FU-4](#fu-4-young-idle-connections-are-protected-from-a-real-login) Young idle connections are protected from a real login | auto-start | minor (wake can be blocked) | M | Done |
| 5 | [FU-5](#fu-5-refused-players-in-a-burst-sometimes-get-no-message) Refused players in a burst sometimes get no message | auto-start | minor | S | Done |
| 6 | [FU-6](#fu-6-one-policy-for-processes-the-launch-script-leaves-behind) One policy for processes the launch script leaves behind (covers 3 findings) | lifecycle | minor | M | Done |
| 7 | [FU-7](#fu-7-monitor-shutdown-during-wait-for-the-previous-launch-script) Monitor shutdown during "wait for the previous launch script" | lifecycle | minor | S | Done |
| 8 | [FU-8](#fu-8-a-re-shown-tab-can-show-live-during-an-outage) A re-shown tab can show "Live" during an outage | web UI | minor (cosmetic) | S | Done |

Effort: S is under half a day, M is about a day, both including tests.

- Items 1–5 are independent and can be done in parallel.
- Item 6 is one design change.
- After each item, run the tests and lint:
  ```bash
  uv run poe test
  uv run poe smoke
  uv run poe lint
  ```
- For the listener items, also re-run the flood scenarios (described per
  item) against a live monitor with the dummy server.

---

## FU-1: X-Forwarded-Port with non-ASCII digits crashes requests

- **Severity:** minor. An anonymous client can flood the log and get HTTP 500.
- **Where:** `src/web_server.py`, `_apply_forwarded_headers`, the check
  `if port.isdigit() and 0 < int(port) < 65536:`.
- **Symptom:** set up as in the README, with a trusted reverse proxy. Any client
  sends `X-Forwarded-Port: ²`; the superscripts ¹ ² ³ arrive as Latin-1 bytes
  0xB9/0xB2/0xB3. nginx passes client headers through by default.
  - Every such request gets HTTP 500 and logs a ~1 KB traceback.
  - The error happens before the rate limiter, and a trusted proxy is exempt
    from the per-address connection cap. So one client can cycle the rotated
    `monitor.log` (4 × 5 MB) in under a minute, pushing out the login and
    server-action history.
- **Cause:** `str.isdigit()` is True for superscript digits, but `int()`
  rejects them.
- **Fix:**
  1. Parse strictly with `re.fullmatch(r"[0-9]{1,5}", port)` (or
     `port.isascii() and port.isdigit()`), and ignore anything else.
  2. Apply the same strict parsing to every forwarded header that is turned
     into a number.
  3. In the README's nginx block, add `proxy_set_header X-Forwarded-Port "";`
     and `proxy_set_header X-Forwarded-Host "";`, so clients can't inject these
     headers through the trusted proxy.
- **Tests:** a unit test sending `X-Forwarded-Port` values `²`, `٣` (Arabic-Indic
  digit), `65536`, `-1`, `8443` and `abc` from a trusted proxy address. Only
  `8443` is used; nothing raises, and no traceback is logged.
- **Done.**
  - `_apply_forwarded_headers` parses ports with `_port_number()`: ASCII
    digits only, 1–65535. Anything else is ignored.
  - The `X-Forwarded-Host` port goes through the same check. Its regex no
    longer uses `\d`, which also matches other scripts' digits.
  - The forwarded client address is normalized (`str(ip_address(...))`). An
    IPv6 scope id after `%` is free text and could carry control characters
    into the log.
  - The README's nginx block clears `X-Forwarded-Host` and
    `X-Forwarded-Port` and explains why.
  - Tests:
    - `test/test_web_server.py`: strict port and host parsing, client
      normalization;
    - `test/integration/test_web_request.py`: `TestProxyOrigins`, the six
      values through the full app; `TestMalformedProxyHeaders`, raw Latin-1
      and UTF-8 header bytes through the real server: HTTP 200, no error and
      no traceback.

## FU-2: Invalid player names are logged verbatim

- **Severity:** minor (log forging, terminal escape injection).
- **Where:** `src/auto_start.py`. `_handle` logs `outcome.player_name` for
  refused logins, and `_RefusalLog.note` logs the raw name.
- **Symptom:** anyone who can reach the game port sends a Login Start whose name
  contains control characters, such as `a\nFAKE ERROR` or `\x1b[31mRED\x1b[0m`.
  Those characters are exactly what make the name invalid.
  - The newline starts a forged line in `monitor.log` and on the console.
  - ANSI escapes reach the operator's terminal or journal.
  - The rate limit allows one such name every 10 s.
- **Fix:** escape names before logging, e.g. `name.encode("unicode_escape",
  "backslashreplace").decode("ascii")` or `repr(name)`. Apply it wherever a
  client-supplied string is logged: player names and the handshake address.
  The client IP comes from the socket and is safe. Consider a small `safe_for_log()` helper in
  `src/minecraft/protocol.py` that both the listener and the web server use.
- **Tests:** feed Login Start names containing `\n`, `\r` and `\x1b[…`. Assert
  the log record is a single line with no control characters.
- **Done.**
  - `safe_for_log()` in `src/minecraft/protocol.py` escapes backslashes and
    every character `str.isprintable()` rejects: C0/C1 controls, NEL, line
    and paragraph separators, bidi overrides. It cuts text at 200 characters.
  - The listener uses it for every logged player name: refusals, the wake
    line, "joined while starting" and the access-check failure in
    `sleeping.py`. Valid names can still hold invisible non-ASCII
    characters, so all of these needed it.
  - The web server uses it for the URL path and `Host` in the cross-origin
    and CSRF warnings. A path can carry `%0A` or `%1B`.
  - The handshake address is not logged anywhere, so it needed no change.
  - Tests:
    - `test/test_protocol.py`: `safe_for_log` cases;
    - `test/integration/test_auto_start_listener.py`: names with `\n`, `\r`
      and ANSI escapes over the real protocol, plus a waking name with
      NEL/U+2028/U+202E. Every log record is printable, so one line;
    - `test/integration/test_web_request.py`: a forged path.

## FU-3: Legacy pings are never evicted

- **Severity:** minor. Under a deliberate flood, players can't wake the server.
- **Where:** `src/auto_start.py`. `ConnectionSlots._victim` evicts only the
  phases `status`, `refused`, `pending` and `login`. `_handle` promotes a legacy
  ping (0xFE) to phase `legacy`, and `sleeping._finish` holds the slot for
  about 1 s after the reply.
- **Symptom:** an attacker holds connections that send `0xFE 0x01` and
  reconnects when they are dropped.
  - Players share one address (a tunnel such as playit.gg, or NAT): 4 such
    connections from that address fill its per-address cap. Every join from it
    gets "Too many connections" for as long as the attack lasts.
  - From 32 addresses × 4 they fill the global cap.
  - IPv6 is keyed by /56, so one /48 host has 256 keys.
  - Round 2 woke the server on the first attempt in both cases.
- **Fix:**
  1. Put `legacy` in the cheap tier with `status` and `refused`.
  2. Better, invert the rule: every phase except an *allowed* login is
     evictable, so a new phase can't silently become unevictable again.
- **Tests:** a unit test where a slot held by a legacy ping is evicted by a
  complete allowed login. An integration test where 4 held legacy pings from
  the joining player's address don't block the wake.
- **Done (the inverted rule).**
  - `ConnectionSlots._victim` treats every phase except a request in progress
    (`pending`, `login`) and an `allowed` login as cheap. Legacy pings, and
    any phase added later, are evictable.
  - Tests:
    - `test/test_auto_start.py::test_every_phase_but_an_allowed_login_is_evictable`
      (`status`, `legacy`, `refused` and an unknown phase);
    - `test/integration/test_auto_start_listener.py::test_held_legacy_pings_do_not_block_a_wake`
      (fails on the old code).

## FU-4: Young idle connections are protected from a real login

- **Severity:** minor. Under a deliberate flood from the players' shared
  address, the wake can be blocked.
- **Where:** `src/auto_start.py`. The second tier of `ConnectionSlots._victim`,
  `('pending', 'login')`, applies the 0.25 s grace even when a complete login
  needs room. `admit_login` uses it.
- **Symptom:** behind a shared address, 8–16 connections that send nothing (or
  one byte) and reconnect immediately keep all 4 per-address slots filled with
  pending entries younger than 0.25 s. A real player's join was then refused
  for 1–21 attempts, and in some runs never got through in 20 s. Round 2 let a
  complete login evict any pending entry; the round-3 fix for same-address
  join bursts added the grace.
- **Fix:** when a complete login needs room and no cheap entry exists, evict a
  young pending entry that has **no bytes buffered**. Check that with
  `sock.recv(1, MSG_PEEK | MSG_DONTWAIT)`; `EAGAIN` means empty. Protect only
  young pending entries whose request is already waiting in the socket buffer:
  that's the burst case S6 was about.
- **Tests:**
  - A unit test with fake sockets: empty young pending entries are evicted for
    a login; ones with buffered data are spared.
  - An integration test: 8 reconnecting idle lanes from the player's address,
    and the join wakes the server within 2 attempts.
- **Done, with three changes to the proposal:**
  - **FIONREAD instead of a `MSG_PEEK` recv.** A `recv(1, MSG_PEEK |
    MSG_DONTWAIT)` through a Python socket that has a timeout first waits out
    that timeout (measured: 1.5 s), under the slots lock. `bytes_waiting()`
    asks the kernel with `FIONREAD`; Windows uses a zero-timeout `select()`.
  - **Requests being delivered are spared, not only buffered ones.** Real
    clients send the handshake and Login Start as two writes, and a client
    has sometimes not sent anything yet in the first milliseconds after
    accept. The burst test showed both being evicted with a reset. A young
    pending connection is now spared while bytes wait unread, or while it
    connected or sent its first byte less than `request_stall` (0.1 s) ago.
    The handler's read and the time it records are one step under the lock
    the evictor's probe takes, so the kernel can't hand over bytes unseen.
    Idle lanes are evicted at once, never-sent first; stalled ones
    (one-byte lanes) after 0.1 s.
  - **The login waits rather than being told "busy".** A complete login
    that finds only connections spared this way waits in the triage once,
    for at most 0.1 s (`ConnectionSlots.retry_at`), then evicts one that is
    still silent. A flood that refreshes every slot at once can't turn the
    player away.
  - Victims of this tier are shut down for reading only. A request that
    completed just as it was evicted is answered "busy" instead of reset.
  - Tests:
    - `test/test_auto_start.py`: fake-probe unit tests for idle vs.
      buffered/arriving entries, the just-connected grace, `retry_at`, the
      atomic read, and `bytes_waiting` on a real socket pair;
    - `test/integration/test_auto_start_listener.py::test_idle_lanes_do_not_block_a_wake`:
      8 lanes from the player's address; the default grace and a long grace
      (every lane young). Both fail on the old code.

## FU-5: Refused players in a burst sometimes get no message

- **Severity:** minor (UX: a few refused players see "connection lost" instead
  of "not white-listed").
- **Where:** `src/auto_start.py`. The `is_allowed` wrapper in `_serve_request`
  marks the entry `refused` before `sleeping._serve_login` sends the
  disconnect. A complete login from the triage then evicts `refused`/`status`
  entries with grace 0 (`_victim`). The victim's `abort()` calls
  `shutdown(SHUT_RDWR)`, so the pending reply fails with EPIPE.
- **Symptom:** when 12–20 refused players (not whitelisted or banned) join
  through one address in a burst, about 1–5 % get EOF and no message. Allowed
  players are unaffected: in the same tests, 0 of ~200 got EOF.
- **Fix (any one of these):**
  - Mark the entry `refused` only after the reply has been sent.
  - Give `refused` entries a short grace (e.g. 0.1 s) before they can be
    evicted.
  - Abort cheap entries with `shutdown(SHUT_RD)`, so a reply already in
    progress still goes out.
- **Tests:** a burst test with non-whitelisted names (12 × 30 joins) in which
  every join gets a message.
- **Done (`SHUT_RD`).**
  - Evicted connections are shut down for reading only. That still wakes the
    handler's blocked read, while a reply being sent (a refusal, a status)
    goes out.
  - A login evicted during its access check also gets its answer.
  - Only a `pending` connection older than the grace (stalled before its
    request) still gets `SHUT_RDWR`.
  - Tests:
    - `test/integration/test_auto_start_listener.py::test_a_burst_of_refused_players_all_get_a_message`:
      12 × 30 non-whitelisted joins through one address. Every join gets the
      refusal or "busy" and none wakes the server. It failed on the old code
      in 3 of 3 runs.
    - The listener tests passed 30 of 30 repeated runs, half of them next to
      two CPU-bound processes.

## FU-6: One policy for processes the launch script leaves behind

The current behaviour, after round 3:
- Processes alive while the server ran, other than the server itself, are
  "background helpers".
- They are ended (SIGTERM, then SIGKILL 10 s later) `min(stopTimeout, 30 s)`
  after the server stops.
- The launch script itself, and anything it starts after the server exits,
  are never touched.

That fixed the round-2 problems, but three gaps remain.

| Finding | Severity | What goes wrong |
|---|---|---|
| Post-stop work inside a long-lived pipeline (S1, partially fixed) | minor | `run.sh` runs `( java …; ./backup.sh ) | tee run.log`, or uses `exec > >(tee -a run.log)`. The `tee`/subshell existed while the server ran, so it counts as a helper and is killed after 30 s. A backup running in that pipeline is cut short (the script dies on SIGPIPE), and a Restart relaunches right after the kill. Round 1 let these layouts finish. |
| Pending cleanup lost on monitor shutdown | minor | A helper scheduled for cleanup lives only in memory. If the monitor stops within the grace (e.g. "stop the server from the dashboard, then stop the monitor", as the README suggests), the helper runs forever and the next wake starts a second copy. |
| Cleanup kills a helper the next launch reused | minor | A script that reuses its helper ("start the tunnel only if it isn't running") keeps the old one when a player rejoins within the grace. The old server's timer then kills it under the new server: the server runs without its tunnel or backup loop. |

- **Where:** `src/process_manager.py`: `_schedule_cleanup` / `_run_cleanup`
  (the in-memory cleanup set, advanced by `observe()`), `close()`, and
  `launch_process` / `_await_previous_launcher`.
- **Proposed design:**
  1. **Track helpers per launch generation:** `(pid, boot-relative start
     time)`.
  2. **Never end anything while that generation's launcher is still alive.**
     Post-stop work, pipelines included, belongs to the launcher.
  3. **End a generation's helpers when its launcher has exited,** or at the
     latest **right before the next launch spawns** (after the existing "wait
     for the previous launch script"). Use SIGTERM, 10 s, then SIGKILL. Then a
     new launch never finds an old helper to reuse, and never has one killed
     underneath it.
  4. **Bound the waiting time.** If a launcher is still alive `stopTimeout`
     after the server stopped and no launch is pending, log a warning and
     leave it alone (it's the user's script). Only processes from a
     generation whose launcher has exited are ever ended.
  5. **Survive monitor restarts.**
     - On `close()`, end helpers whose launcher has exited.
     - Persist the rest (pid plus start key) to `data/pending-cleanup.json`,
       and finish them on the next start once their launcher is gone.
     - Ignore entries whose start key no longer matches (PID reuse).
  6. **Document it** in the README ("Modded server tips"): background
     processes started by the launch script are ended after the server and
     the launch script have both exited.
- **Tests:**
  - `run.sh` with `( dummy; sleep 3; touch marker ) | cat`: the marker appears
    after a Stop, an auto-stop and a Restart, and the Restart relaunches only
    after the marker exists.
  - `exec > >(tee -a run.log)` followed by 40 one-second backup steps: they all
    complete.
  - A helper that the next launch reuses survives the old server's cleanup.
  - Monitor stopped within the grace: the next monitor ends the helper before
    launching, so there is no second copy.
  - The existing post-stop-work, background-helper and lifecycle-1 tests keep
    passing.
- **Done, as proposed:**
  - **Per launch.** `clear_identity()` records what the ended launch leaves:
    its launch script while it still runs, plus the helpers that ran with
    the server, as `(pid, start key)`.
  - **Nothing ended while the launch script runs.** Helpers of a launch are
    ended (SIGTERM, 10 s, SIGKILL) once its script has exited. That happens
    on the next detection scan while no server runs, or at the latest
    synchronously in `launch_process()`. There it follows the
    (bounded, abortable) wait for the previous launch script and comes
    before the spawn.
  - **Bounded.**
    - A launch script still running `stopTimeout` after the stop is warned
      about once and left alone.
    - If it still runs when the next launch starts anyway, its helpers are
      left alone too (logged): the new server may use them.
    - Adopting a server does the same.
  - **Monitor restarts.**
    - `close()` ends the helpers of exited launch scripts (waiting up to 1 s
      for SIGTERM) and saves the rest to `data/pending-cleanup.json`.
    - The next start loads that file and drops entries whose process is
      gone or whose start key no longer matches.
    - The next launch also waits for a saved launch script.
  - **Docs.** README ("Modded server tips", the `data/` files),
    `settings.yaml.example` and `docs/ARCHITECTURE.md`.
  - Tests in `test/integration/test_process_manager.py`:
    - `TestPipelineLaunchScript`: `( server; sleep 3; touch marker ) | cat`.
      The marker appears after a Stop and an auto-stop, with `stopTimeout` 1
      (the old 1 s grace killed the pipeline). A Restart relaunches only
      after the marker.
    - `TestProcessSubstitutionLaunchScript`: `exec > >(tee -a run.log)`
      followed by 40 short steps printing through `tee`; all complete. The
      steps are 0.1 s each; the old grace was set to 1 s, so they outlast it.
    - `TestLeftoverHelpers`:
      - a tunnel that the script starts "only if it isn't running": the
        next launch's tunnel survives, because the old one was ended first;
      - `close()` ends it once the launch script has exited;
      - the monitor is stopped during post-stop work: the next monitor
        waits for the old script, ends the saved tunnel before launching,
        and only one tunnel exists.
  - Tests in `test/test_process_manager.py` (`TestLeftovers`), with faked
    liveness and signals: the order of signals, the lingering warning,
    save and reload, reused PIDs, unusable files, adoption, and a launch
    script that outlives the wait.
  - Seven of the new integration tests fail on the old code. The
    pipeline-restart test is a regression guard and passes on both.

## FU-7: Monitor shutdown during "wait for the previous launch script"

- **Severity:** minor.
- **Where:** `src/process_manager.py`, `_await_previous_launcher`. It has no
  shutdown check and logs nothing while waiting. It runs on the process
  worker, which `LifecycleRuntime.close()` (`src/lifecycle/runtime.py`) joins
  for up to `stopTimeout + max(30, stopTimeout) + 60` s.
- **Symptom:** a Restart, or a wake soon after a stop, waits for `run.sh`'s
  post-stop backup. If the admin stops the monitor during that wait:
  - it is silent for up to `stopTimeout` (22 s in the test, up to 120 s by
    default);
  - it then **launches the server as it exits**, leaving the server
    unsupervised until another monitor adopts it.
- **Fix:**
  1. Make the wait abortable by an `Event` that `close()` sets.
  2. When it is set, skip the launch: return `None`, so the supervisor commits
     STOPPED, and log "Shutting down: not starting the server".
  3. Log progress while waiting, e.g. every 10 s: "Still waiting for the
     previous launch script (pid N)".
- **Tests:** start a Restart while the previous launcher sleeps 10 s, call
  `close()` after 1 s. It must return within 2 s, with no new server process.
- **Done.**
  - `LifecycleRuntime` hands its closing event to the process manager
    (`watch_shutdown`). `close()` sets that event *before* it joins the
    process worker; the process manager's own `close()` sets another.
  - The wait for the previous launch script and the wait for leftover
    helpers both stop at once when either event is set. `launch_process()`
    then returns None and logs "Shutting down: not starting the server".
  - While waiting, it logs "Still waiting for the previous launch script
    (pid N)" every 10 s.
  - Stops are still never cut short.
  - Tests:
    - `test/integration/test_process_manager.py::TestShutdownWhileWaitingForTheLaunchScript`:
      a real Supervisor, LifecycleRuntime and OSProcessManager with the
      lifecycle fakes. `close()` returns in under 2 s, no second server is
      found, and run.sh is left alone. On the old code `close()` took 9 s.
    - `test/test_process_manager.py::test_shutdown_skips_a_launch_that_waits`.

## FU-8: A re-shown tab can show "Live" during an outage

- **Severity:** minor (cosmetic, lasts 0.5–2.5 s).
- **Where:** `src/static/dashboard.js`:
  - `onChannelMessage` updates `live.leaderSeenAt` from heartbeats even while
    the tab is hidden.
  - `startLive()`'s timeout then calls `becomeFollower()` when that heartbeat
    is less than 5 s old, even though no leader answered this tab's `hello`.
    A leader whose stream is down now stays silent.
  - `becomeFollower()` hard-codes `setConnection('live', 'Live')`.
- **Symptom:** a tab brought to the front during the first ~4.5 s of a monitor
  outage shows "Live" for 0.5–2.5 s while the other tabs show "Reconnecting…".
  It happened in 6 of 6 trials. The general outage case is fixed: every
  already-visible tab shows "Reconnecting…".
- **Fix:**
  1. Follow only if a `leader` or `payload` message arrived after this tab's
     `hello`: record `helloAt` and require `leaderSeenAt > helloAt`. Otherwise
     become the leader.
  2. In `becomeFollower()`, show the relayed connection state, or
     "Connecting…" until the first relay, instead of "Live".
- **Tests:** a Playwright test that cuts the monitor, re-shows a hidden tab
  within 1 s, and asserts it never shows "Live" until the monitor is back.
- **Done.**
  - `startLive()` records `helloAt`. Its timeout follows only if a `leader`
    or `payload` message arrived since then (`leaderSeenAt > helloAt`), and
    otherwise becomes the leader.
  - `becomeFollower(relayed)` shows the leader's relayed connection state,
    or "Connecting…" until the first relay.
  - `mirrorConnection()` ignores unknown kinds.
  - Tests:
    - `test/e2e/test_live_tabs.py`: an in-process WebServer, a leader tab
      and a tab opened hidden. The test cuts the monitor and re-shows the
      hidden tab right away. It records every connection label: no "Live"
      during the outage, then "Live" in both tabs once the monitor is back.
    - On the old code the hidden tab went `Paused → Connecting… → Live →
      Reconnecting…`.

---

## Known limitations that need a real environment

These can't be settled inside this repository's test sandbox.

- **No test against a real Minecraft server yet.** Running a downloaded server
  jar was not allowed in the build sandbox. All end-to-end tests use the stdlib
  dummy server, plus the independent `mcstatus` library for the protocol.
  Before the first release, follow the "Release QA" steps in CONTRIBUTING.md
  with a real Forge/NeoForge or Fabric server: join while it sleeps,
  auto-stop, adoption after a monitor restart, and kick/ban/unban.
- **Windows paths have only been unit-tested:** spawn flags, `cmd.exe`
  quoting of `run.bat` commands, `SO_EXCLUSIVEADDRUSE`, stdin `stop` through
  `cmd.exe`, and the `pause` at the end of Forge's `run.bat`. The GitHub
  Actions CI matrix includes `windows-latest`. Check the first CI run, and do
  one manual Windows run with a Forge server.
- **By design:**
  - **Accounts aren't verified:** the sleeping server can't verify Mojang
    accounts, so someone who knows a whitelisted name can wake (but not join)
    the server.
  - **Restart-loop scripts:** a launch script that restarts the server in a
    loop brings it back after a Stop. The monitor warns about such scripts at
    every startup.
  - **Shared-address connection caps:** these apply to players behind one
    address (NAT/tunnel): 4 concurrent connections to the sleeping listener
    and 24 to the dashboard.
- **The `wip/qa-fixes` branch can be deleted.** It still exists on GitHub
  (deleting it from this session was not permitted), and everything in it is
  on `main`.
