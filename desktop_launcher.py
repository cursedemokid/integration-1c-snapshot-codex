"""Run IntegrationOneS in a dedicated browser window.

The FastAPI server is stopped when the dedicated Chrome, Edge, or Chromium
window is closed. The normal browser profile is never reused.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn

from main import app


APP_NAME = "IntegrationOneS"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
STARTUP_TIMEOUT_SECONDS = 15


class DesktopLauncherError(RuntimeError):
    pass


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def configure_working_directory() -> Path:
    application_dir = application_directory()
    configured_env = os.environ.get("INTEGRATION_ONES_ENV")
    env_path = Path(configured_env).resolve() if configured_env else application_dir / ".env"
    if not env_path.is_file():
        raise DesktopLauncherError(
            f"Файл настроек не найден: {env_path}. "
            "Поместите .env рядом с приложением."
        )
    os.chdir(env_path.parent)
    return env_path


def browser_candidates(system: str | None = None) -> tuple[str, ...]:
    system = system or platform.system()
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("PROGRAMFILES", "")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "")
        return tuple(
            candidate
            for candidate in (
                str(Path(program_files_x86) / "Microsoft/Edge/Application/msedge.exe") if program_files_x86 else "",
                str(Path(program_files) / "Google/Chrome/Application/chrome.exe") if program_files else "",
                str(Path(local) / "Google/Chrome/Application/chrome.exe") if local else "",
                "msedge.exe",
                "chrome.exe",
            )
            if candidate
        )
    if system == "Darwin":
        return (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        )
    return (
        "google-chrome",
        "google-chrome-stable",
        "microsoft-edge",
        "microsoft-edge-stable",
        "chromium",
        "chromium-browser",
    )


def find_browser(system: str | None = None) -> str:
    configured_browser = os.environ.get("INTEGRATION_ONES_BROWSER")
    if configured_browser:
        configured_path = Path(configured_browser).expanduser()
        if configured_path.is_file():
            return str(configured_path.resolve())
        resolved = shutil.which(configured_browser)
        if resolved:
            return resolved
        raise DesktopLauncherError(
            f"Указанный браузер не найден: {configured_browser}"
        )

    for candidate in browser_candidates(system):
        path = Path(candidate)
        if path.is_absolute() and path.is_file():
            return str(path)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise DesktopLauncherError(
        "Не найден поддерживаемый браузер. Установите Chrome, Edge или Chromium."
    )


def browser_command(executable: str, url: str, profile_directory: str) -> list[str]:
    return [
        executable,
        f"--app={url}",
        f"--user-data-dir={profile_directory}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--disable-component-update",
        "--no-service-autorun",
        "--REDACTED-store=basic",
    ]


def select_port(host: str, preferred_port: int) -> int:
    for port in (preferred_port, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return int(sock.getsockname()[1])
    raise DesktopLauncherError("Не удалось выбрать свободный локальный порт.")


def wait_for_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if server.started:
            return
        if not thread.is_alive():
            break
        time.sleep(0.05)
    server.should_exit = True
    thread.join(timeout=5)
    raise DesktopLauncherError("Локальный сервер не запустился.")


def stop_server(server: uvicorn.Server, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=5)


def run() -> int:
    configure_working_directory()
    host = DEFAULT_HOST
    requested_port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    port = select_port(host, requested_port)
    browser = find_browser()
    url = f"http://service.example.invalid/"

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    server_thread = threading.Thread(target=server.run, name="integration-ones-server", daemon=True)
    server_thread.start()
    wait_for_server(server, server_thread)

    try:
        with tempfile.TemporaryDirectory(prefix="integration-ones-browser-") as profile_directory:
            browser_process = subprocess.Popen(
                browser_command(browser, url, profile_directory),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                return browser_process.wait()
            except KeyboardInterrupt:
                browser_process.terminate()
                try:
                    browser_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    browser_process.kill()
                return 130
    finally:
        stop_server(server, server_thread)


def main() -> None:
    try:
        raise SystemExit(run())
    except (DesktopLauncherError, ValueError) as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
