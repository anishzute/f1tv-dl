"""Desktop interaction tests; run under xvfb-run on a headless Linux machine."""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_models import FIXTURE, URL

from f1tv_dl.desktop import DesktopService, VideoInfo
from f1tv_dl.errors import AuthenticationError, OperationCancelled
from f1tv_dl.gui import DownloaderApp, tk
from f1tv_dl.media import DownloadProgress
from f1tv_dl.models import Content, ContentRef


@unittest.skipIf(tk is None, "Tk is not installed")
class DesktopInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            root = tk.Tk()
            root.destroy()
        except tk.TclError:
            raise unittest.SkipTest("No display; use xvfb-run for desktop tests") from None

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.service = DesktopService(session_path=self.folder / "session.json")
        self.service.auth = Mock()
        self.service.auth.password_login.return_value = "gui-session"
        self.content = Content.from_response(ContentRef.parse(URL), FIXTURE)
        self.service.load_video = Mock(return_value=self.content)
        self.root = tk.Tk()
        with patch.dict(os.environ, {"F1TV_USER": ""}):
            self.app = DownloaderApp(self.root, self.service)
        self.errors = patch("f1tv_dl.gui.messagebox.showerror")
        self.showerror = self.errors.start()
        self.addCleanup(self.errors.stop)
        self.addCleanup(self.app._destroy)
        self.root.update()

    def wait_idle(self):
        deadline = time.monotonic() + 4
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(self.app.busy, "The UI worker did not finish")

    def login(self):
        self.app.method.set("Email and password")
        self.app._account_fields()
        self.app.email.set("test@example.test")
        self.app.password.set("private-password")
        self.app.signin_button.invoke()
        self.wait_idle()

    def load(self):
        self.app.url.set(URL)
        self.app.load_button.invoke()
        self.wait_idle()

    def test_graphical_login_feed_selection_download_and_sign_out(self):
        self.assertTrue(self.app.download_button.instate(["disabled"]))
        self.login()
        self.assertEqual(self.app.password.get(), "")
        self.assertEqual(self.app.tabs.select(), str(self.app.download_tab))
        self.load()
        self.assertEqual(len(self.app.feeds.get_children()), 26)
        driver = next(i for i, c in enumerate(self.content.channels) if c.title == "VER")
        self.app.feeds.selection_set(str(driver))
        self.root.update()
        self.app.directory.set(str(self.folder))
        self.app.international.set("Dutch")

        def fake_download(content, options, **callbacks):
            callbacks["progress"](DownloadProgress(10, 1048576, "3x"))
            result = options.directory / "gui-download.mp4"
            result.write_bytes(b"completed video")
            return result

        self.service.download = Mock(side_effect=fake_download)
        self.app.download_button.invoke()
        self.wait_idle()
        options = self.service.download.call_args.args[1]
        self.assertEqual(options.channel, "VER")
        self.assertEqual(options.international, "nld")
        self.assertEqual(self.app.progress_text.get(), "Download complete")
        self.assertTrue(self.app.last_output.exists())
        self.assertTrue(self.app.open_button.instate(["!disabled"]))
        self.app.sign_out()
        self.wait_idle()
        self.assertFalse(self.service.signed_in)
        self.assertTrue(self.app.download_button.instate(["disabled"]))
        self.showerror.assert_not_called()

    def test_cancel_keeps_the_window_responsive(self):
        self.login()
        self.load()
        started = threading.Event()

        def waiting_download(content, options, *, cancel, **kwargs):
            started.set()
            cancel.wait(3)
            if cancel.is_set():
                raise OperationCancelled()
            raise AssertionError("Cancellation never reached the worker")

        self.service.download = Mock(side_effect=waiting_download)
        self.app.start_download()
        self.assertTrue(started.wait(1))
        self.root.update()
        self.app.cancel_button.invoke()
        self.wait_idle()
        self.assertIn("Cancelled", self.app.status.get())
        self.assertTrue(self.app.download_button.instate(["!disabled"]))
        self.showerror.assert_not_called()

    def test_failed_login_displays_error_and_keeps_download_disabled(self):
        self.service.auth.password_login.side_effect = AuthenticationError("Login rejected")
        self.login()
        self.assertFalse(self.service.signed_in)
        self.assertTrue(self.app.download_button.instate(["disabled"]))
        self.showerror.assert_called_once()

    def test_quality_inspection_updates_dropdowns(self):
        self.login()
        self.load()
        self.service.inspect = Mock(return_value=VideoInfo(("1920x1080", "640x360"), ("nld",)))
        self.app.inspect_button.invoke()
        self.wait_idle()
        self.assertIn("1920x1080", self.app.quality_combo["values"])
        self.assertEqual(self.app.audio.get(), "Dutch")

    def test_loading_another_url_invalidates_the_previous_video(self):
        self.login()
        self.load()
        self.service.load_video.side_effect = AuthenticationError("Video unavailable")
        self.app.load_button.invoke()
        self.wait_idle()
        self.assertIsNone(self.app.content)
        self.assertTrue(self.app.download_button.instate(["disabled"]))

    def test_folder_picker_and_playback_link_are_graphical(self):
        with patch("f1tv_dl.gui.filedialog.askdirectory", return_value=str(self.folder)):
            self.app.choose_folder()
        self.assertEqual(self.app.directory.get(), str(self.folder))
        self.login()
        self.load()
        self.service.stream_url = Mock(return_value="https://cdn.test/play.mpd?token=private")
        self.app.link_button.invoke()
        self.wait_idle()
        self.assertEqual(self.root.clipboard_get(), "https://cdn.test/play.mpd?token=private")
        self.assertNotIn("private", self.app.log.get("1.0", "end"))

    def test_cancel_and_status_remain_visible_in_a_small_window(self):
        self.root.geometry("820x640")
        self.app.status.set("A long status message should wrap and keep Cancel visible. " * 4)
        self.root.update()
        self.assertTrue(self.app.cancel_button.winfo_viewable())
        bottom = (
            self.app.cancel_button.winfo_rooty()
            - self.root.winfo_rooty()
            + self.app.cancel_button.winfo_height()
        )
        self.assertLessEqual(bottom, self.root.winfo_height())

    def test_close_still_works_if_the_task_finishes_during_confirmation(self):
        self.app._run("Working…", lambda cancel: None)

        def confirm(*args, **kwargs):
            self.wait_idle()
            return True

        with (
            patch("f1tv_dl.gui.messagebox.askyesno", side_effect=confirm),
            patch.object(self.app, "_destroy") as destroy,
        ):
            self.app.close()
        destroy.assert_called_once()
