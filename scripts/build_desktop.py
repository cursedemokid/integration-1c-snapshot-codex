"""Build the one-click IntegrationOneS desktop launcher with PyInstaller."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "IntegrationOneS"


def build() -> Path:
    try:
        import PyInstaller.__main__
    except ImportError as exc:
        raise SystemExit(
            "PyInstaller не установлен. Выполните: "
            "python -m pip install -r requirements-build.txt"
        ) from exc

    arguments = [
        str(ROOT / "desktop_launcher.py"),
        "--name",
        APP_NAME,
        "--onefile",
        "--noconfirm",
        "--clean",
        "--paths",
        str(ROOT),
        "--add-data",
        f"{ROOT / 'index.html'}{os.pathsep}.",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build" / "desktop"),
        "--specpath",
        str(ROOT / "build"),
    ]
    if os.name == "nt":
        arguments.append("--windowed")

    PyInstaller.__main__.run(arguments)
    executable = ROOT / "dist" / (f"{APP_NAME}.exe" if os.name == "nt" else APP_NAME)
    if not executable.is_file():
        raise SystemExit(f"Сборка не создана: {executable}")
    shutil.copy2(ROOT / ".env.example", ROOT / "dist" / ".env.example")
    print(f"Сборка готова: {executable}")
    return executable


def run_built_application(executable: Path) -> int:
    environment = os.environ.copy()
    local_env = ROOT / ".env"
    if local_env.is_file():
        environment["INTEGRATION_ONES_ENV"] = str(local_env)
    return subprocess.run([str(executable)], env=environment, check=False).returncode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Run the application after a successful build")
    args = parser.parse_args()
    executable = build()
    if args.run:
        raise SystemExit(run_built_application(executable))


if __name__ == "__main__":
    main()
