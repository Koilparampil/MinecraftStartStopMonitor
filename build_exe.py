"""Build a single-file minecraft-monitor executable into dist/ with PyInstaller.

Usage: uv run poe build-exe   (or: uv run python build_exe.py [extra PyInstaller args])
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(ROOT, "minecraft-monitor.spec")


def main(argv=None):
    extra = list(sys.argv[1:] if argv is None else argv)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", *extra, SPEC],
        check=True,
        cwd=ROOT,
    )
    name = "minecraft-monitor.exe" if os.name == "nt" else "minecraft-monitor"
    print(f"Build complete. dist/ contains: {name}")
    print("Put settings.yaml next to it (or run it once for the interactive setup).")


if __name__ == "__main__":
    main()
