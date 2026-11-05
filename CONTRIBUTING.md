# Contributing to Minecraft Monitor

## Development setup

```bash
git clone https://github.com/Koilparampil/minecraft-monitor.git
cd minecraft-monitor
uv sync
```

## Running tests

Unit and integration tests (no Minecraft server needed):

```bash
uv run poe test
```

Linting and formatting:

```bash
uv run poe lint
uv run poe format
```

## Smoke tests (e2e)

The smoke tests launch the full monitor (`python -m src.main`) and exercise it
through a real browser against the dummy Minecraft server
(`test/dummy/minecraft_dummy.py`). Everything runs on free ports in temporary
folders, and teardown only stops the processes the tests started. They are
safe on a machine that also runs a real server.

1. Install the Playwright browser once:

   ```bash
   uv run playwright install chromium
   ```

2. Run the suite:

   ```bash
   uv run poe smoke
   ```

What they cover:

- `test_autostart`: a raw handshake + login on the game port wakes the
  dummy server.
- `test_autostop`: an empty server is stopped after the startup guard and
  `stopDelay`, observed through the committed SSE states.
- `test_web_ui`: log in, start, then loading → online and the player list,
  kick/ban/unban, console, restart and stop through the browser.

## Release QA

Before tagging a release:

1. Run `uv run poe test`, `uv run poe smoke` and `uv run poe build-exe`.
2. Check the dashboard by hand against the dummy server:
   - login/logout, start/stop/restart, the auto-stop countdown;
   - kick, ban and unban, both online and offline (server stopped);
   - the console and the mod list;
   - light/dark themes and a phone-sized window.
3. Against a real modded server you can afford to restart (e.g. a copy of your
   world):
   - join from the Minecraft client while it sleeps. You should see the
     sleeping MOTD in the server list, then the "starting" message on join,
     and be able to join once it has loaded;
   - leave and wait for auto-stop. Confirm the log shows the world being saved
     and that the monitor listens again;
   - kill the monitor while the server runs and start it again. It must adopt
     the running server, not launch a second one.
4. Tag `vX.Y.Z`. The release workflow builds the Windows and Linux binaries.
