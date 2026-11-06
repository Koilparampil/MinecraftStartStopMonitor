# minecraft-monitor — architecture & build contract

`minecraft-monitor` mirrors [kevinnio/palworld-monitor](https://github.com/kevinnio/palworld-monitor)
(MIT) for **modded Minecraft Java servers** (Forge, NeoForge, Fabric, Quilt, vanilla):

1. **Auto-start** — while the server is stopped, the monitor listens on the game
   port and pretends to be a *sleeping* server: the multiplayer list shows a
   "sleeping" MOTD; a player's **join attempt** wakes the real server (the player
   is disconnected with "server is starting, reconnect shortly").
2. **Auto-stop** — when the server has had **zero players for `stopDelay`
   seconds** (fresh, valid samples only), it is stopped **gracefully** (world saved).
3. **Web GUI** — authenticated dashboard with live state (SSE): start / stop /
   restart, players (kick / ban / unban), auto-stop countdown, server log console
   with RCON commands, mod list.

These three are paramount. The reference implementation was studied in full; its
architecture (single-writer lifecycle supervisor + runtime threads + pure
auto-stop policy + committed snapshots with monotonic revisions) is **kept**. What
changes is everything Palworld-specific (UDP magic packet, REST API, PalServer.exe,
steam IDs, banlist.txt, LGSM).

## Repository layout and ownership

Every file has exactly one owner. **Edit only files you own.** If you need a
contract change, adapt on your side and report it to the lead.

| Owner | Files |
|---|---|
| **lead** | `README.md`, `AGENTS.md`, `CONTRIBUTING.md`, `docs/*`, `pyproject.toml`, `uv.lock`, `.gitignore`, `LICENSE`, `.github/workflows/*`, `src/events.py`, `src/lifecycle/messages.py`, `test/conftest.py`, `test/support/__init__.py`, `test/support/net.py`, `test/support/waiting.py` |
| **protocol** | `src/minecraft/protocol.py`, `src/minecraft/ping.py`, `src/minecraft/rcon.py`, `src/minecraft/client.py`, `src/minecraft/server_files.py`, `src/minecraft/sleeping.py`, `test/dummy/minecraft_dummy.py`, `test/support/dummy_server.py`, `test/test_protocol.py`, `test/test_ping.py`, `test/test_rcon.py`, `test/test_minecraft_client.py`, `test/test_server_files.py`, `test/test_sleeping.py`, `test/integration/test_dummy_server.py` |
| **lifecycle** | `src/lifecycle/supervisor.py`, `src/lifecycle/runtime.py`, `src/lifecycle/auto_stop.py`, `src/process_manager.py`, `src/minecraft_control.py`, `test/support/fakes.py`, `test/test_lifecycle_supervisor.py`, `test/test_lifecycle_auto_stop.py`, `test/test_process_manager.py`, `test/integration/test_lifecycle_runtime.py`, `test/integration/test_minecraft_control.py`, `test/integration/test_process_manager.py` |
| **app-shell** | `src/settings.py` (extend only; attribute names are a contract), `src/settings.yaml.example`, `src/cli.py`, `src/setup_wizard.py`, `src/main.py`, `src/auto_start.py`, `src/player_manager.py`, `src/banlist_manager.py`, `deploy/minecraft-monitor.service`, `build_exe.py`, `minecraft-monitor.spec`, `test/test_settings.py`, `test/test_cli.py`, `test/test_setup_wizard.py`, `test/test_auto_start.py`, `test/test_player_manager.py`, `test/test_banlist_manager.py`, `test/integration/test_auto_start_listener.py`, `test/integration/test_main.py` |
| **web** | `src/web_server.py`, `src/auth.py`, `src/templates/*`, `src/static/*`, `test/test_web_server.py`, `test/test_auth.py`, `test/integration/test_web_request.py`, `test/e2e/*` |

Stubs exist for every cross-owner module (`raise NotImplementedError`) so imports
work from minute one. Replace the stub bodies; keep the public signatures.

## Ground rules for every agent

- Python ≥ 3.11, run everything with `uv run …` from the repo root. Dependencies
  are already installed (`uv sync` done). **Do not** run `uv add`/`uv sync`/`uv lock`
  or edit `pyproject.toml`; if you need a package, say so in your report.
- **Do not** `git commit`, branch, stash, reset or checkout. The lead commits.
- Run only your own tests while building: `uv run pytest test/test_x.py …`.
  Lint/format only your own files: `uv run ruff check <files>`,
  `uv run ruff format <files>`.
- Imports use the `src.` package prefix (`from src.settings import settings`),
  the app runs as `python -m src.main`.
- Read settings at call time through the singleton, never copy values at import.
- No new global state beyond the `settings` singleton. Pass collaborators in.
- Keep the copyright header style of the reference on files that are clearly
  ported from it (Nomomo / Kevin Perez / Koilparampil lines, MIT).
- Logging via `logging`; never log passwords (`SENSITIVE_SETTINGS`).

### Testing rules (enforced in review)

- **Never hard-code ports.** Use `test.support.free_tcp_port()` / `free_tcp_ports(n)`.
  Several agents run tests at the same time on this machine.
- Never write outside `tmp_path`. Use the `mock_settings` fixture (it points
  `serverDir` and `dataDir` into `tmp_path`) and `in_tmp_cwd` when code writes
  cwd-relative files.
- **Never kill processes you did not start** (the reference e2e suite killed every
  PalServer on the host — do not copy that). Kill only PIDs your fixture spawned,
  always in `finally`/fixture teardown.
- Unit tests fast (< 1 s each). Mark anything > 5 s `@pytest.mark.slow`.
- Lifecycle tests use fakes + an injected clock; no sleeps for timing logic.

## Contracts

### Settings (`src/settings.py`)

Attribute names, defaults and the nested YAML mapping are defined in
`src/settings.py` (`DEFAULTS`, `YAML_MAPPING`). Important ones:

| attribute | YAML | meaning |
|---|---|---|
| `serverDir` | `minecraft.serverDir` | server root (server.properties, mods/, world/, logs/) |
| `startCommand` | `minecraft.startCommand` | launch command run with cwd=`serverDir`; **str** (split with `shlex` on POSIX) **or list** |
| `serverHost` | `minecraft.host` | host the monitor uses for RCON/ping (default 127.0.0.1) |
| `serverPort` | `minecraft.port` | game port; `None` → server.properties `server-port` → 25565 |
| `rconPort`, `rconPassword` | `minecraft.rconPort`, `.rconPassword` | `None` → server.properties (`enable-rcon=true` required for the password) |
| `pollingRate` | `minecraft.pollingRate` | seconds between polls (5) |
| `stopTimeout` | `minecraft.stopTimeout` | seconds to wait for a graceful save+stop before SIGTERM/kill (120) |
| `processName` | `minecraft.processName` | discovery filter, substring of process name/exe/cmdline (`java`) |
| `listenerBindHost` | `autoStart.bindHost` | `None` → server.properties `server-ip` → `""` (all interfaces) |
| `sleepingMotd`, `startingMessage`, `sleepingVersionName`, `respectWhitelist` | `autoStart.*` | sleeping-server presentation and access policy |
| `autoStopDelay` | `autoStop.stopDelay` | seconds empty before auto-stop (600) |
| `webBindHost`, `webServerPort` (25580), `webUsername`, `webPassword`, `controlServerThroughWeb`, `showServerIPAddress`, `webConsole`, `playerAvatars` | `web.*` | dashboard |
| `dataDir` | `monitor.dataDir` | monitor state dir (`data/`): `players.json`, `server.pid`, `server-console.log`; use `settings.data_path(name)` |

`settings.resolve_server_properties()` fills `None` ports/password/bind host from
`server.properties`; `main` calls it after CLI overrides, before
`validate_settings()`.

### Shared types (`src/lifecycle/messages.py`) — read the file

`LifecycleState`, `ProcessIdentity`, `ProcessSnapshot`, `ProcessObservation`,
`OnlinePlayer`, `Roster`, `LaunchRequest/Result`, `PollRequest`, `RosterSample`
(**`roster: Optional[Roster]`**, `None` = unavailable), `StopRequest/Result`,
`StartCommand`, `StopCommand`, **`RestartCommand`**, `ActionResult`.

### Events (`src/events.py`)

Same bus as the reference. Payloads:

| event | publisher | payload |
|---|---|---|
| `LIFECYCLE_CHANGED` | supervisor | `{state, generation, revision}` (STARTING, STOPPING, RUNNING-after-failed-stop) |
| `SERVER_STARTED` | supervisor | `{revision, pid, generation, state: "RUNNING"}` |
| `SERVER_STOPPED` | supervisor | `{revision, pid, generation, state: "STOPPED"}` |
| `SERVER_STATUS` | supervisor | `{revision, state, playerData, playerCount, generation}` after every roster sample |
| `PLAYER_JOINED` / `PLAYER_LEFT` | PlayerManager | `{name, uuid}` |
| `BAN_ADDED` / `BAN_REMOVED` | BanlistManager | `{name, uuid}` |

Consumers never drive lifecycle state; they re-read `controller.get_snapshot()`.

### Committed snapshot (`controller.get_snapshot()`)

Built by the supervisor under its state lock. The web UI and SSE send exactly this:

```json
{
  "revision": 12,
  "state": "RUNNING",
  "ready": true,
  "playerData": "ok",
  "playerCount": 2,
  "maxPlayers": 20,
  "players": [{"name": "Steve", "uuid": "8667ba71-…", "currently_online": true, "last_online": 1762300000.0, "first_seen": 1762200000.0}],
  "banned_players": [{"name": "Griefer", "uuid": "…", "reason": "Banned by an operator.", "source": "Server", "created": "2026-11-05 10:00:00 +0000", "expires": "forever"}],
  "server": {"version": "1.20.1", "motd": "A Modded Server", "source": "rcon"},
  "startedAt": 1762300000.0,
  "autoStopAt": 1762300600.0,
  "restartPending": false
}
```

- `ready`: the server answered (RCON or ping) at least once in the current
  RUNNING generation. RUNNING + `ready=false` = "process up, still loading mods".
- `playerData`: `unknown` (no sample yet this generation) | `ok` | `unavailable`
  (last sample failed; roster shows last-known data).
- `playerCount`: `Roster.online` of the last valid sample (authoritative, includes
  hidden players); `0` unless RUNNING. `maxPlayers`/`server.*` last known or `null`.
- `startedAt`, `autoStopAt`: **wall-clock** epoch seconds (`time.time()` domain)
  for the UI; `autoStopAt` is `null` unless an auto-stop countdown is armed.
- `revision` is monotonic per controller; the UI ignores older snapshots.

### MinecraftClient (`src/minecraft/client.py`) — protocol

Thread-safe; one persistent RCON connection guarded by a lock, reconnect on error.

- `get_roster() -> Optional[Roster]`:
  1. If RCON configured: `list uuids`; if the reply is an unknown-command error or
     has no UUIDs, use `list` (remember the capability). Parse with a tolerant
     parser covering 1.13+ (`There are 2 of a max of 20 players online: A, B`),
     1.7–1.12 (`There are 2/20 players online:` + names on following line(s)),
     `list uuids` (`Name (uuid)`), Bukkit-ish hybrids (`out of maximum`), with
     `§x` codes stripped. Unparseable → treat RCON as failed for this sample.
  2. Else / on RCON failure: status ping → `Roster(online=players.online,
     players=<named sample entries, excluding anonymous/zero-UUID ones>,
     max_players, source="ping", version, motd)`.
  3. Both fail → `None`. Never raises.
  When RCON answers, attach `version`/`motd` from a status ping cached ≤ 60 s
  (best effort — a failed ping never fails the roster).
- `run_command(cmd) -> str` (raises `RconUnavailable`/`RconError`), multi-packet
  replies supported (vanilla splits at 4096 bytes; terminate with a sentinel
  request — vanilla answers unknown packet types with `Unknown request <hex>`).
- `kick_player/ban_player/unban_player(name[, reason]) -> ActionResult` (RCON
  `kick`, `ban`, `pardon`); success inferred from the reply (case-insensitive
  `Kicked`/`Banned`/`Unbanned`, "already banned"/"isn't banned" count as
  success-with-message). The controller re-verifies bans against the file.
- `request_stop() -> bool` (RCON `stop`; connection drop after sending = sent).
- `rcon_status`: `disabled` | `unknown` | `ok` | `auth_failed` | `unreachable`
  (constants `RCON_*` in `src.minecraft.client`). After a rejected password
  the client stops trying RCON for 60 s, doubling to 10 min, while the status
  ping keeps the roster alive. It logs one error per state change, and
  console/moderation calls fail fast with the remaining wait.
- Player counts (ping and `list`) must be bounded ASCII integers; anything
  else makes the sample unavailable (`None`), never a low count.
- `close()`.

### server_files (`src/minecraft/server_files.py`) — protocol

See the stub docstrings: `read_properties`, `update_properties` (preserve
comments/order/unknown keys, Java-properties escaping, atomic write),
`read_json_list`, `write_json_list` (atomic), `offline_uuid`, `list_mods`,
`read_server_icon`, `read_eula`, `write_eula`, `tail_file`.

### Sleeping server (`src/minecraft/sleeping.py`) — protocol

`handle_connection(conn, client_ip, config, is_allowed, timeout=5.0) -> ConnectionOutcome`
serves **one** accepted connection:

- First byte `0xFE` → legacy ping: reply `0xFF` kick packet
  (`§1\0<proto>\0<version>\0<motd>\0<online>\0<max>`, UTF-16BE) → `LEGACY_PING`.
- Handshake (`0x00`: VarInt protocol, String(255) address — may carry Forge
  markers like `\0FML2\0` / `\0FORGE`, UShort port, VarInt next_state).
  - next_state 1 → read Status Request, send Status Response JSON
    (`version{name, protocol: config.protocol_version or client's}`,
    `players{max, online: 0, sample: []}`, `description{text: motd}`, optional
    `favicon`), then answer Ping (`0x01` int64) with Pong → `STATUS`.
  - next_state 2 or 3 → read Login Start (`0x00`, String(16) name first in every
    version), call `is_allowed(name, ip)`; allowed → Login Disconnect (`0x00`,
    JSON text `{"text": kick_message}`) and `wake=True`; denied → Disconnect with
    the deny message, `wake=False` → `LOGIN`.
- Limits: frame ≤ 32 KiB, per-connection timeout, never raises → `INVALID`.
- After the reply: `shutdown(SHUT_WR)` and briefly drain until the client closes,
  so the client sees the message and the client side holds TIME_WAIT.

### Process manager (`src/process_manager.py`) — lifecycle

Passive backend (no threads, no events). API in the stub. Semantics:

- `launch_process()`: spawn `settings.startCommand` with `cwd=settings.serverDir`,
  `stdin=PIPE` (kept for graceful stop), stdout+stderr → `settings.data_path("server-console.log")`
  (truncated per launch). POSIX: `start_new_session=True`. Windows:
  `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`; `.bat`/`.cmd` via `cmd.exe /c`.
  Keep the `Popen` so it can be reaped (`poll()`) and written to. Record identity
  (PID + create_time) in `settings.data_path("server.pid")`.
- `find_process_identity()`: discovery for adoption — a live, non-zombie process
  whose **cwd resolves to `serverDir`** and whose name/exe/any cmdline element
  contains `settings.processName` (case-insensitive); prefer the tracked identity;
  never `os.getpid()`.
- `terminate_process(expected)` — graceful chain, blocking (runs on the process
  worker): (1) `stop\n` to our stdin pipe if we own the process, else
  `graceful_stopper()` (RCON stop); (2) wait up to `settings.stopTimeout` for the
  process tree to exit; (3) terminate the tree (SIGTERM — the JVM shutdown hook
  saves; on Windows this is a hard kill, log a warning), wait 30 s; (4) kill.
  Returns True gone / False still alive. Compare-and-clear identity as the reference.
- `observe()`, `identity_is_running()` (zombie = not running; reap our Popen),
  `set_known_identity`, `clear_identity`, `close()` (close pipes, do **not** stop
  the server).
- `create_process_manager(graceful_stopper)` picks the OS backend.

### Supervisor / runtime / auto-stop — lifecycle

Port the reference's `supervisor.py`, `runtime.py`, `auto_stop.py` and keep their
guarantees (single writer, generations, stale-observation discard, fresh-empty
-sample requirement, startup guard, failed-stop recovery). Changes:

- `RosterSample.roster`: `Roster` or `None`; auto-stop counts `roster.online`.
- `ready` flag per generation; `_online_count`, `_max_players`, server info kept
  from the last valid roster; reset on every start/stop commit.
- `RestartCommand`: RUNNING → begin stop with `restart_pending`; when the exit is
  committed (stop result **or** observed exit) publish `SERVER_STOPPED` then
  immediately begin a start (bypass the stop→start cooldown, keep the generation
  rules). STOPPED → same as start. Otherwise rejected. A failed stop clears the
  pending restart.
- Snapshot fields above (`ready`, `maxPlayers`, `server`, `startedAt`,
  `autoStopAt`, `restartPending`) with an injectable `wall_clock` for tests.
- Runtime threads are named `minecraft-lifecycle`, `minecraft-detection`,
  `minecraft-poll`, `minecraft-process`; process worker calls
  `process_manager.launch_process()` / `terminate_process(identity)`; poll worker
  calls `client.get_roster()`; process-worker join timeout ≥ `stopTimeout + 60`.

### MinecraftController (`src/minecraft_control.py`) — lifecycle

Facade (API in the stub). Moderation rules:

- `kick_player`: RUNNING + RCON only.
- `ban_player` / `unban_player`: RUNNING + RCON → `client.ban_player`/`unban_player`,
  then `banlist.refresh()` and **verify** via `banlist.is_banned(name)` (success iff
  the file agrees). STOPPED → `banlist.add_ban_offline(name, uuid=player_manager.find_uuid(name) or banlist.lookup_uuid(name), reason)` /
  `remove_ban_offline(name)`. STARTING/STOPPING, or RUNNING without RCON → failure
  with a clear message (never edit the file while the server may be running).
- `run_console_command(cmd)`: RUNNING + RCON; returns the output.
- Every failure returns `ActionResult(success=False, message=...)`; nothing raises.

### PlayerManager / BanlistManager — app-shell (APIs in the stubs)

- Players keyed by **lowercase name**; record `{name, uuid, currently_online,
  last_online, first_seen}`; persisted to `settings.data_path("players.json")` (or
  the injected `data_file`) as `{"version": 1, "players": {...}}`; don't rewrite the
  file on every poll when nothing changed (save on change, otherwise ≤ once/60 s).
- `banned-players.json` is vanilla format (`[{uuid, name, created, source, expires, reason}]`);
  the server writes it on `/ban` and `/pardon`. Offline edits are atomic,
  vanilla-formatted, require a UUID (from usercache.json / tracked players; fall
  back to `offline_uuid(name)` **only** if `server.properties` has
  `online-mode=false`), otherwise fail with "start the server to ban this player".

### AutoStartManager (`src/auto_start.py`) — app-shell

Mirrors the reference manager (subscribe `SERVER_STARTED` → stop listening,
`SERVER_STOPPED` → listen, `LIFECYCLE_CHANGED` STARTING → release port; `close()`
unsubscribes). TCP instead of UDP:

- Bind `(settings.listenerBindHost or "", settings.serverPort)`; dual-stack IPv6
  when binding all interfaces and available; `SO_REUSEADDR` on POSIX,
  `SO_EXCLUSIVEADDRUSE` on Windows; bind retries like the reference.
- Accept loop with a short timeout so it notices aborts; each connection handled in
  a daemon thread (cap ~16 concurrent) via `sleeping.handle_connection`.
- Config per listen cycle: `motd=settings.sleepingMotd`, `version_name`,
  `protocol_version=None`, `max_players` from server.properties (`max-players`,
  default 20), `favicon=read_server_icon(serverDir)`, `kick_message=settings.startingMessage`.
- `is_allowed(name, ip)`: if not `respectWhitelist` → allowed. Else deny banned
  names (banned-players.json) and IPs (banned-ips.json); if server.properties
  `white-list=true`, allow only names in whitelist.json **or ops.json**
  (case-insensitive).
- The first `wake` outcome: close the listening socket (release the port), then
  `controller.start_server()` (exactly once per listen cycle).

### WebServer (`src/web_server.py`) — web

Flask, port the reference (Flask-Login, Flask-WTF CSRF, Flask-Limiter,
`LoginAttemptTracker`, SSE broadcast under one lock with revision ordering).
Fix the reference's open redirect (only relative `next` paths) and compare
credentials with `hmac.compare_digest`. Routes:

| route | auth | purpose |
|---|---|---|
| `GET/POST /login`, `POST /logout` | – / ✓ | session login (CSRF-protected form) |
| `GET /` | ✓ | dashboard (initial snapshot rendered server-side) |
| `POST /action` `action=startServer\|stopServer\|restartServer\|getStatus` | ✓ | → `{success, message, data: snapshot, autoStopDelay}`; 400 on unknown action; control actions 403 when `controlServerThroughWeb` is false |
| `POST /kick`, `/ban`, `/unban` `player=<name>[&reason=]` | ✓ | name must match `^[A-Za-z0-9_.*-]{1,32}$` else 400 → `{success, message, data}` |
| `GET /banned` | ✓ | `{success, banned_players}` |
| `GET /stream` | ✓ (401 JSON) | SSE `data: {"data": snapshot, "autoStopDelay": N}` + keepalive comments |
| `GET /console?source=log\|launch&lines=N` | ✓ + `webConsole` | tail of `serverDir/logs/latest.log` (`log`) or `server-console.log` (`launch`) → `{success, source, lines}` |
| `POST /command` `command=...` | ✓ + `webConsole` + `controlServerThroughWeb` | RCON command (single line, ≤ 256 chars, leading `/` stripped) → `{success, message, output}` |
| `GET /info` | ✓ | `{mods, modCount, icon, autoStart, autoStop, autoStopDelay, serverAddress?}` (cached ~30 s) |
| `GET /healthz` | – | `{"ok": true}` (no sensitive data) |

`/stream`, `/healthz`, `/console` and static files are exempt from the default rate
limit. UI: state pill (OFFLINE / STARTING / LOADING (RUNNING∧¬ready) / ONLINE /
STOPPING), start/stop/restart with in-page confirm dialogs (no native
`confirm()`), auto-start/auto-stop panel with a live countdown from `autoStopAt`,
players table (avatar optional via `playerAvatars`), console panel, mods panel,
light/dark theme, mobile friendly. Original artwork only — no Mojang assets.

### Setup wizard / CLI / main — app-shell

- CLI flags: `--settings`, `--server-dir`, `--start-command`, `--host`,
  `--server-port`, `--rcon-port`, `--rcon-password`, `--polling-rate`,
  `--stop-timeout`, `--no-web`, `--web-port`, `--web-username`, `--web-password`,
  `--trusted-proxies`, `--secure-cookies`, `--no-auto-start`, `--no-auto-stop`,
  `--auto-stop-delay` (`--help` is authoritative). Settings path: `--settings` →
  `$MINECRAFT_MONITOR_SETTINGS` → `settings.yaml` next to the executable
  (frozen) / in `src/`. Monitor files (`monitor.log`, `session_secret.key`,
  `dataDir`) resolve relative to the settings file's folder.
- First run (no settings file):
  - With a TTY, the interactive wizard runs. CLI values (`--server-dir`,
    `--start-command`, `--web-password`) prefill or skip their questions.
  - Without a TTY, a complete command line is used instead: `--server-dir`,
    `--start-command`, and `--web-password` or `--no-web`. Those values are
    validated and saved to `settings.yaml` (0600, wizard format; server files
    untouched), so later runs need no flags. Otherwise the monitor prints how
    to create the settings and exits 1.
  - Before asking anything, the wizard checks that `settings.yaml` is
    writable. It saves `settings.yaml` (0600) *before* editing
    `server.properties` or `eula.txt`, so cancelling anywhere changes nothing.
  - Launch-command detection: `run.sh`/`run.bat` for Forge/NeoForge ≥ 1.17 →
    `./run.sh nogui`; `fabric-server-launch.jar` / `quilt-server-launch.jar` /
    `forge-*.jar` (not `-installer`) / `minecraft_server*.jar` / `server.jar`
    → `java -Xms1G -Xmx<mem> -jar <jar> nogui`. It warns about modpack start
    scripts that restart in a loop, and the same warning runs at every startup.
  - It offers to enable RCON in server.properties: `enable-rcon=true`,
    `rcon.port`, a random `rcon.password`, and **`broadcast-rcon-to-ops=false`**
    so polling doesn't spam ops.
  - EULA: if not accepted, ask explicitly (link https://aka.ms/MinecraftEULA) and
    write `eula=true` only on "yes".
  - The dashboard port avoids the game, RCON and query ports. The password
    `changeme` is refused, both at the prompt and at startup.
- `main`: banner, logging (stdout + `monitor.log`), settings load → CLI overrides →
  `resolve_server_properties()` → validate; build `MinecraftClient(serverHost,
  serverPort, rconPort, rconPassword)`, `EventBus`, process manager
  (`graceful_stopper=client.request_stop`), managers, controller (`start()`),
  auto-start (arm only if no server process is discovered), web server (blocks);
  with `--no-web` block until SIGINT/SIGTERM (the reference exited immediately).
  Shutdown closes web → auto-start → controller → client; it never stops the
  Minecraft server (it is adopted on the next start).

### Dummy Minecraft server (`test/dummy/minecraft_dummy.py`) — protocol

A **stdlib-only, independent** implementation (do not import `src.*`: it doubles
as a cross-check of our protocol code). Run with cwd = a server dir:

- Reads `./server.properties`: `server-port`, `enable-rcon`, `rcon.port`,
  `rcon.password`, `max-players`, `motd`, `white-list`, `hide-online-players`.
- Env: `DUMMY_PLAYERS` (comma list, default `TestPlayer1,TestPlayer2,TestPlayer3`,
  empty = nobody), `DUMMY_STARTUP_DELAY` (s before ports open / "Done"),
  `DUMMY_STOP_DELAY` (s "saving", default 0.3), `DUMMY_IGNORE_STOP=1` (ignore
  stop + SIGTERM), `DUMMY_LIST_FORMAT=modern|legacy`, `DUMMY_VERSION` (1.20.1),
  `DUMMY_NO_UUIDS=1` (`list uuids` unknown, like old servers).
- Serves status ping + ping/pong, rejects logins with a disconnect, RCON (auth,
  multi-packet replies, `Unknown request <hex>`), commands `list`, `list uuids`,
  `kick`, `ban`, `pardon`, `banlist`, `say`, `save-all`, `stop`, plus test hooks
  `dummy players a,b,c` (replace the online roster) and `dummy crash` (exit 1).
  Same commands on stdin. Maintains vanilla-format `banned-players.json` and
  `usercache.json` (offline UUIDs). Writes vanilla-style lines to `logs/latest.log`
  and stdout (`Starting minecraft server version …`, `Done (…s)! For help, type "help"`,
  `RCON running on …`, `Stopping server`, `Saving worlds`). SIGTERM = graceful stop.
- `test/support/dummy_server.py`: `write_server_files(server_dir, game_port,
  rcon_port, rcon_password="dummy-rcon", **props)`, `dummy_start_command() -> list[str]`
  (`[sys.executable, <abs path to minecraft_dummy.py>]`), `DUMMY_PROCESS_NAME =
  "minecraft_dummy"` (use as `settings.processName`), and a `run_dummy_server(...)`
  context manager that spawns it directly, waits until RCON answers, and always
  kills it on exit.

## Definition of done (each builder)

1. No `NotImplementedError` left in owned files; public APIs match this document.
2. Owned tests written and passing (`uv run pytest <owned tests>`), following the
   testing rules; ruff check + format clean on owned files.
3. Final report (≤ 40 lines): what was built, test command + result, deviations
   from this contract and why, anything other owners must know, open risks.
