import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import desktop_launcher


class DesktopLauncherTests(TestCase):
    def test_browser_command_uses_app_window_and_isolated_profile(self) -> None:
        command = desktop_launcher.browser_command(
            "/usr/bin/google-chrome",
            "http://127.0.0.1:8000/",
            "/tmp/profile",
        )

        self.assertEqual(command[0], "/usr/bin/google-chrome")
        self.assertIn("--app=http://127.0.0.1:8000/", command)
        self.assertIn("--user-data-dir=/tmp/profile", command)
        self.assertIn("--disable-background-mode", command)

    @patch("desktop_launcher.shutil.which")
    def test_find_browser_uses_first_available_linux_browser(self, which) -> None:
        which.side_effect = lambda name: "/usr/bin/chromium" if name == "chromium" else None

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(desktop_launcher.find_browser("Linux"), "/usr/bin/chromium")

    def test_find_browser_uses_configured_executable(self) -> None:
        with patch.dict(os.environ, {"INTEGRATION_ONES_BROWSER": "/bin/true"}):
            self.assertEqual(desktop_launcher.find_browser("Linux"), str(Path("/bin/true").resolve()))

    @patch("desktop_launcher.shutil.which", return_value=None)
    @patch.object(Path, "is_file", return_value=False)
    def test_find_browser_reports_missing_browser(self, _is_file, _which) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(desktop_launcher.DesktopLauncherError, "Chrome, Edge или Chromium"):
                desktop_launcher.find_browser("Linux")

    @patch("desktop_launcher.socket.socket")
    def test_select_port_falls_back_to_an_available_port(self, socket_class) -> None:
        sock = socket_class.return_value.__enter__.return_value
        sock.bind.side_effect = [OSError("busy"), None]
        sock.getsockname.return_value = ("127.0.0.1", 43123)

        self.assertEqual(desktop_launcher.select_port("127.0.0.1", 8000), 43123)
        self.assertEqual(sock.bind.call_args_list[0].args[0], ("127.0.0.1", 8000))
        self.assertEqual(sock.bind.call_args_list[1].args[0], ("127.0.0.1", 0))

    def test_configure_working_directory_uses_explicit_env_path(self) -> None:
        env_path = Path(__file__).resolve().parents[1] / ".env.example"
        previous = Path.cwd()
        try:
            with patch.dict(os.environ, {"INTEGRATION_ONES_ENV": str(env_path)}):
                self.assertEqual(desktop_launcher.configure_working_directory(), env_path)
                self.assertEqual(Path.cwd(), env_path.parent)
        finally:
            os.chdir(previous)
