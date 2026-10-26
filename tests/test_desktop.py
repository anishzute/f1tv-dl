import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_models import FIXTURE, URL

from f1tv_dl.desktop import DesktopService, DownloadOptions, install_browser_support
from f1tv_dl.errors import AuthenticationError, F1TVError, OperationCancelled
from f1tv_dl.models import Content, ContentRef


class DesktopServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "session.json"
        self.service = DesktopService(session_path=self.path)
        self.service.auth = Mock()
        self.service.auth.password_login.return_value = "password-session"
        self.service.auth.browser_login.return_value = "browser-session"
        self.content = Content.from_response(ContentRef.parse(URL), FIXTURE)

    def test_password_login_remembers_only_the_token_and_signs_out(self):
        self.service.sign_in("password", username="user", password="private-password")
        self.assertTrue(self.service.signed_in)
        self.assertEqual(self.service.store.load(), "password-session")
        self.assertNotIn("private-password", self.path.read_text())
        self.service.sign_out()
        self.assertFalse(self.service.signed_in)
        self.assertFalse(self.path.exists())

    def test_remember_disabled_does_not_write_a_session(self):
        self.service.sign_in("password", username="user", password="secret", remember=False)
        self.assertTrue(self.service.signed_in)
        self.assertFalse(self.path.exists())

    def test_turning_remember_off_clears_a_previously_saved_session(self):
        self.service.sign_in("password", username="user", password="secret")
        self.assertTrue(self.path.exists())
        self.service.sign_in("password", username="user", password="secret", remember=False)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.service.signed_in)

    def test_token_sign_in_is_not_saved_even_if_remember_is_checked(self):
        self.service.sign_in("token", token="explicit-token", remember=True)
        self.assertTrue(self.service.signed_in)
        self.assertFalse(self.path.exists())
        with self.assertRaises(AuthenticationError):
            self.service.sessions.refresh()

    def test_failed_sign_in_preserves_the_previous_session(self):
        self.service.sign_in("token", token="previous")
        self.service.auth.password_login.side_effect = AuthenticationError("Rejected")
        with self.assertRaises(AuthenticationError):
            self.service.sign_in("password", username="user", password="secret")
        self.assertEqual(self.service.sessions.get(), "previous")

    def test_saved_session_is_loaded_on_next_launch(self):
        self.service.sign_in("password", username="user", password="secret")
        other = DesktopService(session_path=self.path)
        self.assertTrue(other.signed_in)
        self.assertEqual(other.sessions.get(), "password-session")

    def test_expired_gui_login_requests_sign_in_without_retaining_password(self):
        self.service.sign_in("password", username="user", password="secret")
        with self.assertRaisesRegex(AuthenticationError, "Account tab"):
            self.service.sessions.refresh()
        self.assertEqual(self.service.auth.password_login.call_count, 1)

    def test_browser_login_passes_cancellation_and_custom_executable(self):
        cancel = threading.Event()
        self.service.sign_in("browser", executable="chromium", cancel=cancel)
        self.service.auth.browser_login.assert_called_once_with(
            None,
            None,
            executable="chromium",
            cancel=cancel,
        )

    def test_cancelled_sign_in_does_not_change_session_or_submit_password(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(OperationCancelled):
            self.service.sign_in("password", username="user", password="secret", cancel=cancel)
        self.service.auth.password_login.assert_not_called()

    def test_quality_inspection_returns_actual_resolutions_and_languages(self):
        self.service.stream_url = Mock(return_value="https://cdn.test/clear.mpd")
        self.service.http = Mock()
        self.service.http.get_text.return_value = "<MPD/>"
        streams = {
            "streams": [
                {"codec_type": "video", "width": 640, "height": 360},
                {"codec_type": "video", "width": 1920, "height": 1080},
                {"codec_type": "audio", "tags": {"language": "nld"}},
                {"codec_type": "audio", "tags": {"language": "eng"}},
            ]
        }
        with patch("f1tv_dl.desktop.probe", return_value=streams):
            info = self.service.inspect(self.content, "VER")
        self.assertEqual(info.resolutions, ("1920x1080", "640x360"))
        self.assertEqual(info.languages, ("eng", "nld"))

    def test_existing_download_fails_before_playback_authorization(self):
        output = self.root / "2026-united-states-grand-prix-VER.mp4"
        output.write_bytes(b"original")
        self.service.stream_url = Mock()
        with patch("f1tv_dl.desktop.require_binary", return_value="ffmpeg"):
            with self.assertRaises(F1TVError):
                self.service.download(self.content, DownloadOptions(self.root, channel="VER"))
        self.service.stream_url.assert_not_called()
        self.assertEqual(output.read_bytes(), b"original")

    def test_invalid_options_are_rejected_before_network_requests(self):
        for options in (
            DownloadOptions(self.root, resolution="bad"),
            DownloadOptions(self.root, offset="00:99:00.000"),
            DownloadOptions(self.root, duration=float("nan")),
            DownloadOptions(self.root, duration=-1),
            DownloadOptions(self.root, format="avi"),
        ):
            with self.subTest(options=options), self.assertRaises(F1TVError):
                options.validate()

    def test_browser_setup_subprocesses_are_never_passed_credentials(self):
        process = Mock(returncode=0)
        process.poll.return_value = 0
        with patch("f1tv_dl.desktop.subprocess.Popen", return_value=process) as launch:
            install_browser_support()
        commands = [call.args[0] for call in launch.call_args_list]
        self.assertEqual(commands[0][-1], "playwright>=1.48,<2")
        self.assertEqual(commands[1][-3:], ["playwright", "install", "chromium"])

    def test_browser_setup_can_be_cancelled_without_starting(self):
        cancel = threading.Event()
        cancel.set()
        with patch("f1tv_dl.desktop.subprocess.Popen") as launch:
            with self.assertRaises(OperationCancelled):
                install_browser_support(cancel)
        launch.assert_not_called()
