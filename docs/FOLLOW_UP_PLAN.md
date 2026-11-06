# Follow-up plan

Open items from the final verification round, with a plan to close each one.

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
- **Nothing critical or major is open.** Every remaining item is a minor edge
  case: deliberate connection floods aimed at the sleeping listener, unusual
  launch-script layouts, a malformed proxy header, or a short UI glitch.
- **Tests.** `uv run poe test` gives 1275 passed and 1 skipped (a Windows-only
  test). `uv run poe smoke` gives 6 passed. Lint is clean.

Severity scale used below (the same as in QA):
- **critical:** data loss, auto-stop with players online, a server that can
  never be woken, or a security hole.
- **major:** a core feature broken in a realistic scenario.
- **minor:** everything else.

## Order of work

| # | Item | Area | Severity | Effort |
|---|------|------|----------|--------|
| 1 | [FU-1](#fu-1-x-forwarded-port-with-non-ascii-digits-crashes-requests) X-Forwarded-Port with non-ASCII digits crashes requests | web | minor (anonymous log flooding) | S |
| 2 | [FU-2](#fu-2-invalid-player-names-are-logged-verbatim) Invalid player names logged verbatim | auto-start | minor (log forging) | S |
| 3 | [FU-3](#fu-3-legacy-pings-are-never-evicted) Legacy pings are never evicted | auto-start | minor (wake can be blocked) | S |
| 4 | [FU-4](#fu-4-young-idle-connections-are-protected-from-a-real-login) Young idle connections are protected from a real login | auto-start | minor (wake can be blocked) | M |
| 5 | [FU-5](#fu-5-refused-players-in-a-burst-sometimes-get-no-message) Refused players in a burst sometimes get no message | auto-start | minor | S |
| 6 | [FU-6](#fu-6-one-policy-for-processes-the-launch-script-leaves-behind) One policy for processes the launch script leaves behind (covers 3 findings) | lifecycle | minor | M |
| 7 | [FU-7](#fu-7-monitor-shutdown-during-wait-for-the-previous-launch-script) Monitor shutdown during "wait for the previous launch script" | lifecycle | minor | S |
| 8 | [FU-8](#fu-8-a-re-shown-tab-can-show-live-during-an-outage) A re-shown tab can show "Live" during an outage | web UI | minor (cosmetic) | S |

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
