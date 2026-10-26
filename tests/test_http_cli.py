import contextlib
import io
import os
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from test_models import FIXTURE, URL

from f1tv_dl.cli import main, parser
from f1tv_dl.config import default_session_path
from f1tv_dl.errors import APIError, F1TVError
from f1tv_dl.http import HttpClient, SafeRedirectHandler, validate_media_url


class HTTPTests(unittest.TestCase):
    def test_temporary_get_errors_are_retried(self):
        client = HttpClient()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = b'{"ok": true}'
        client.opener = Mock()
        client.opener.open.side_effect = [HTTPError(URL, 503, "failure", {}, None), response]
        with patch("f1tv_dl.http.time.sleep"):
            self.assertTrue(client.get_json(URL)["ok"])
        self.assertEqual(client.opener.open.call_count, 2)

    def test_password_post_is_never_retried_or_logged(self):
        client = HttpClient()
        client.opener = Mock()
        client.opener.open.side_effect = HTTPError(URL, 503, "failure", {}, None)
        with self.assertRaises(APIError) as caught:
            client.get_json(URL, body={"Password": "secret"})
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(client.opener.open.call_count, 1)

    def test_network_errors_hide_signed_url(self):
        client = HttpClient()
        client.opener = Mock()
        client.opener.open.side_effect = URLError("https://cdn.test/?token=private")
        with self.assertRaises(F1TVError) as caught:
            client.get_text("https://cdn.test/?token=private")
        self.assertNotIn("private", str(caught.exception))

    def test_cross_origin_redirect_strips_credentials(self):
        request = Request(
            URL,
            headers={"entitlementToken": "private", "apikey": "private", "Accept": "text/plain"},
        )
        redirected = SafeRedirectHandler().redirect_request(
            request, None, 302, "", {}, "https://cdn.test/master.m3u8"
        )
        self.assertFalse(redirected.has_header("Entitlementtoken"))
        self.assertFalse(redirected.has_header("Apikey"))
        self.assertEqual(redirected.get_header("Accept"), "text/plain")

    def test_redirect_downgrade_is_rejected(self):
        with self.assertRaises(F1TVError):
            SafeRedirectHandler().redirect_request(
                Request(URL), None, 302, "", {}, "http://cdn.test/"
            )

    def test_invalid_media_urls_are_rejected(self):
        for url in (
            "file:///private",
            "https://user:password@cdn.test/a",
            "https://cdn.test/a\n",
            "-bad",
        ):
            with self.subTest(url=url), self.assertRaises(F1TVError):
                validate_media_url(url)


class CLITests(unittest.TestCase):
    def test_configured_cache_does_not_evaluate_home(self):
        key = "LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME"
        with (
            patch.dict(os.environ, {key: "/configured-cache"}),
            patch("f1tv_dl.config.Path.home", side_effect=RuntimeError("no home")),
        ):
            self.assertEqual(
                str(default_session_path()).replace("\\", "/"),
                "/configured-cache/f1tv-dl/session.json",
            )

    def test_no_home_directory_disables_default_cache(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("f1tv_dl.config.Path.home", side_effect=RuntimeError("no home")),
        ):
            self.assertIsNone(default_session_path())

    def test_no_cache_parses_without_resolving_a_home(self):
        with patch("f1tv_dl.cli.default_session_path", side_effect=RuntimeError("no home")):
            self.assertTrue(parser().parse_args([URL, "--no-cache"]).no_cache)

    def test_legacy_environment_password_alias_and_new_name(self):
        with patch.dict(os.environ, {"F1TV_PASSWORD": "new", "F1TV_PASS": "old"}, clear=True):
            self.assertEqual(parser().parse_args([URL]).password, "new")
        with patch.dict(os.environ, {"F1TV_PASS": "old"}, clear=True):
            self.assertEqual(parser().parse_args([URL]).password, "old")

    def test_invalid_options_exit_with_two(self):
        for option in (
            ["-s", "1920"],
            ["-s", "0x1"],
            ["--duration", "-1"],
            ["--timeout", "nan"],
            ["--itsoffset=00:99:00.000"],
            ["--channel-list", "--stream-url"],
        ):
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    parser().parse_args([URL, *option])
                self.assertEqual(caught.exception.code, 2)

    def test_negative_audio_offset(self):
        self.assertEqual(
            parser().parse_args([URL, "--itsoffset=-00:00:04.750"]).itsoffset, "-00:00:04.750"
        )

    def test_channel_list_does_not_authenticate_or_require_ffmpeg(self):
        http = Mock()
        http.get_json.return_value = FIXTURE
        out = io.StringIO()
        with (
            patch("f1tv_dl.cli.HttpClient", return_value=http),
            patch("f1tv_dl.cli.Authenticator") as auth,
            patch("f1tv_dl.cli.require_binary") as binary,
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(main([URL, "--channel-list", "--no-cache"]), 0)
        self.assertIn("F1 LIVE", out.getvalue())
        self.assertIn("Max Verstappen", out.getvalue())
        auth.assert_not_called()
        binary.assert_not_called()

    def test_stream_url_stdout_is_machine_readable(self):
        http = Mock()
        http.get_json.side_effect = [
            FIXTURE,
            {
                "resultObj": {
                    "url": "https://cdn.test/master.m3u8?token=secret",
                    "streamType": "HLS",
                }
            },
        ]
        out = io.StringIO()
        with (
            patch("f1tv_dl.cli.HttpClient", return_value=http),
            patch("f1tv_dl.cli.probe") as probe,
            contextlib.redirect_stdout(out),
        ):
            status = main([URL, "--stream-url", "--token", "explicit", "--no-cache"])
        self.assertEqual(status, 0)
        self.assertEqual(out.getvalue(), "https://cdn.test/master.m3u8?token=secret\n")
        probe.assert_not_called()

    def test_invalid_url_returns_nonzero_without_network(self):
        with patch("f1tv_dl.cli.HttpClient") as http, self.assertLogs(level="ERROR"):
            self.assertEqual(main(["https://evil.test/detail/1/test"]), 1)
        http.assert_not_called()

    def test_missing_credentials_fail_noninteractively(self):
        http = Mock()
        http.get_json.return_value = FIXTURE
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("f1tv_dl.cli.HttpClient", return_value=http),
            patch("sys.stdin.isatty", return_value=False),
            self.assertLogs(level="ERROR") as logs,
        ):
            status = main([URL, "--stream-url", "--no-cache"])
        self.assertEqual(status, 1)
        self.assertIn("Login required", " ".join(logs.output))
