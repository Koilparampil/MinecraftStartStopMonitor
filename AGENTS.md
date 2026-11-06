# Minecraft monitor

A Python app that manages a modded Minecraft Java server on its host. It
auto-starts the server when a player tries to join, gracefully auto-stops it
when it is empty, tracks players and bans, and exposes a web dashboard.

Explore the project before making changes. Use `uv` for dependencies and
commands, always `uv run` for Python. [README.md](README.md) covers user-facing
setup and the CLI flags. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the
design contract: snapshot schema, event payloads, module APIs.
[docs/FOLLOW_UP_PLAN.md](docs/FOLLOW_UP_PLAN.md) lists the open follow-up items
from QA, with fixes and tests to add. Read it before touching the listener,
the launch/stop chain or the dashboard's live stream.

## Commands

- `uv sync`: create `.venv` and install deps.
- `uv run poe start`: start the app (first run opens the setup wizard).
- `uv run poe test`: unit + integration tests; pass a file to run one.
- `uv run poe smoke`: e2e tests (`test/e2e/`, Playwright + dummy server).
- `uv run poe lint` / `uv run poe format`: ruff with the pinned rule set.
- `uv run poe build-exe`: standalone binary into `dist/`.

## Architecture

### Composition and lifecycle

- **`src/main.py`**: thin entrypoint and composition root.
  - Order: CLI → first-run wizard → settings (YAML < CLI; unset ports and the
    RCON password come from `server.properties`) → `MinecraftClient` →
    `EventBus` → process manager → player/ban managers →
    `MinecraftController` → `AutoStartManager` → `controller.start()` →
    listener (armed only if no server is found) → `WebServer.run()`.
  - Shutdown runs in reverse and never stops the Minecraft server.
- **`MinecraftController`** (`src/minecraft_control.py`): the facade.
  - start/stop/restart and snapshots.
  - Moderation by committed state: RCON while RUNNING, offline
    `banned-players.json` edits only while STOPPED with no server process
    found.
  - Never raises; failures are `ActionResult`s.
- **Lifecycle models** (`src/lifecycle/messages.py`): data only.
  - `LifecycleState`, process identity/snapshot, `Roster`/`OnlinePlayer`.
  - Requests and results.
  - Start/Stop/Restart commands, `ActionResult`.
- **Supervisor** (`src/lifecycle/supervisor.py`): the only writer of
  lifecycle, roster and auto-stop state.
  - It owns `STOPPED`/`STARTING`/`RUNNING`/`STOPPING`, the generation, a
    monotonic snapshot revision, the per-generation `ready` flag, and the
    pending restart.
  - `handle()`/`on_tick()` are synchronous and return work requests.
  - Stale observations are discarded.
  - It never calls collaborators or the bus while holding its lock.
  - A restart publishes `SERVER_STOPPED` then `LIFECYCLE_CHANGED(STARTING)`
    back to back, and relaunches only if no server process remains.
- **Auto-stop policy** (`src/lifecycle/auto_stop.py`): pure
  deadline/freshness/revalidation decisions with a startup guard.
  - Expiry requires a fresh empty sample.
  - Unavailable samples cancel the countdown.
- **Lifecycle runtime** (`src/lifecycle/runtime.py`): four threads.
  - `minecraft-lifecycle` (calls the supervisor), `minecraft-detection`
    (process scans), `minecraft-poll` (`client.get_roster()`) and
    `minecraft-process` (launch/stop, serialized).
- **Process manager** (`src/process_manager.py`): passive OS backend.
  - Spawns `startCommand` in `serverDir` with a stdin pipe; output goes to
    `data/server-console.log`.
  - Discovers a server by **cwd == serverDir** plus `processName`, and adopts
    it.
  - The graceful stop chain: `stop` on stdin or over RCON → wait
    `stopTimeout` → SIGTERM (a JVM shutdown hook saves; a hard kill on
    Windows) → kill.
  - Owns no threads and never publishes events.

### Minecraft protocol (`src/minecraft/`)

- `protocol.py`: VarInt/String framing, text components, `§` stripping.
- `rcon.py`: persistent, locked RCON client with reconnect and multi-packet
  replies.
- `ping.py`: server-list ping client.
- `client.py`: `MinecraftClient`.
  - `get_roster()` tries RCON `list uuids` → `list` → status ping, and
    returns `None` on failure, never a false empty roster.
  - Moderation, `request_stop()`.
- `sleeping.py`: serves one connection as the sleeping server.
  - Status → MOTD; legacy ping.
  - Login Start → name → access check → disconnect message + `wake`.
- `server_files.py`: Java `.properties` editing that keeps comments, vanilla
  JSON lists, the EULA, mods, the icon, log tails. All writes are atomic.

### Auto-start, players, web, setup

- **`AutoStartManager`** (`src/auto_start.py`): a TCP listener on the game
  port while the committed state is STOPPED.
  - It serves connections via `sleeping.handle_connection` and enforces
    whitelist/ops/bans.
  - On the first allowed join it releases the port, then calls
    `controller.start_server()`.
  - Re-arms on `SERVER_STOPPED` or a rejected start; disarms on
    `SERVER_STARTED`/`STARTING`.
- **`PlayerManager`** / **`BanlistManager`**: name-keyed player history in
  `data/players.json`, and the server's own `banned-players.json` (mtime
  cached; refresh publishes `BAN_ADDED`/`BAN_REMOVED`).
- **`WebServer`** (`src/web_server.py`): Flask with Flask-Login, CSRF and
  rate limiting.
  - Page loads and SSE (`/stream`) read one committed controller snapshot.
    Every committed event re-broadcasts it under one lock, and the UI ignores
    older revisions.
  - Also `/console`, `/command` (RCON), `/info` (mods, icon).
  - UI: `src/templates/`, `src/static/`, vanilla JS, original artwork only.
- **`src/settings.py`**: the settings singleton.
  - Nested YAML ↔ flat attributes, type coercion, validation warnings,
    `server.properties` resolution.
  - `src/settings.yaml.example` documents every key.
- **`src/setup_wizard.py`**: first-run detection of server dir and launch
  command, RCON setup, the EULA prompt (written only on an explicit yes), and
  the dashboard password.

## Testing

- Unit tests in `test/`, integration tests in `test/integration/`, e2e in
  `test/e2e/`. Shared fixtures are in `test/conftest.py`:
  - `mock_settings` patches the real singleton and points
    `serverDir`/`dataDir` at `tmp_path`.
  - `in_tmp_cwd`.
- `test/support/`:
  - `free_tcp_port(s)`; **never hard-code ports**.
  - `wait_until`.
  - `fakes.py`: clocks, process manager, client, player/ban managers.
  - `dummy_server.py`: helpers for the dummy server.
- `test/dummy/minecraft_dummy.py`: a stdlib-only dummy Minecraft server.
  - Status ping, login disconnect, strict vanilla-like RCON, `list`/`kick`/
    `ban`/`pardon`, `banned-players.json`/`usercache.json`, `logs/latest.log`,
    graceful stop on stdin/RCON/SIGTERM.
  - Test hooks `dummy players a,b` and `dummy crash`.
  - It deliberately doesn't import `src`, so it independently cross-checks
    the protocol code (as does `mcstatus` in the tests).
- Lifecycle tests use fakes and injected clocks (`FakeClock`,
  `FakeWallClock`, `OffsetClock`), not sleeps.
- Tests kill only the processes they started, and write only inside
  `tmp_path`.

## Constraints

- Must run on the same host and as the same user as the Minecraft server
  (process spawn/signal, cwd discovery).
- Never edit `banned-players.json` while a server process may be running; the
  server owns that file.
- Never hard-kill a server before `stopTimeout` has elapsed after a
  successful `stop`.

## Commits

- Titles short and to the point; descriptions high-level, in natural language.

## References

- [Java Edition protocol](https://minecraft.wiki/w/Java_Edition_protocol) and
  [Server List Ping](https://minecraft.wiki/w/Java_Edition_protocol/Server_List_Ping):
  consult when touching `src/minecraft/` or the dummy server.
- [RCON](https://minecraft.wiki/w/RCON) and
  [server.properties](https://minecraft.wiki/w/Server.properties).
