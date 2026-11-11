from contextlib import redirect_stdout
from concurrent.futures import Future
import importlib.util
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch
from urllib import error, parse

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("mihomo_select", ROOT / "mihomo_select.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MihomoSelectTest(unittest.TestCase):
    PACKAGE = "mihomo-linux-amd64-v3-v1.19.32.gz"
    DOWNLOAD_URL = "https://github.com/MetaCubeX/mihomo/releases/download/v1.19.32/" + PACKAGE
    SHA256 = "13239bf2a4e9f8e172ee3d3910a5543ac15b7a9e08d3bdefe1cdcfc3d95855ae"

    def run_latency_ui(self, responses, keys):
        proxies = {
            "节点选择": {"type": "Selector", "all": list(responses)},
            **{name: {"type": "Shadowsocks"} for name in responses},
        }

        def respond(req, timeout):
            path = parse.unquote(parse.urlsplit(req if isinstance(req, str) else req.full_url).path)
            if path == "/version":
                return io.BytesIO(b"{}")
            if path == "/proxies":
                return io.BytesIO(json.dumps({"proxies": proxies}).encode())
            name = path.removeprefix("/proxies/").removesuffix("/delay")
            value = responses[name]
            if isinstance(value, list):
                value = value.pop(0)
            if isinstance(value, Exception):
                raise value
            return io.BytesIO(json.dumps(value).encode())

        def submit(func, *args):
            future = Future()
            try:
                future.set_result(func(*args))
            except Exception as exc:
                future.set_exception(exc)
            return future

        executor = MagicMock()
        executor.__enter__.return_value = executor
        executor.submit.side_effect = submit
        screen = Mock()
        screen.getmaxyx.return_value = (24, 160)
        screen.getch.side_effect = keys + [ord("q"), ord("q")]
        frame = {}
        frames = []
        screen.erase.side_effect = frame.clear
        screen.addnstr.side_effect = lambda y, x, text, *args: frame.update({y: text})
        screen.refresh.side_effect = lambda: frames.append(dict(frame))
        opener = Mock()
        opener.open.side_effect = respond
        with patch.object(module.sys, "argv", ["mihomo-select"]), \
                patch.object(module, "setup_locale"), \
                patch.object(module.request, "build_opener", return_value=opener), \
                patch.object(module, "ThreadPoolExecutor", return_value=executor), \
                patch.object(module.curses, "curs_set"), \
                patch.object(module.curses, "wrapper", side_effect=lambda render: render(screen)):
            with self.assertRaises(EOFError):
                module.main()
        return frames

    def test_all_nodes_shows_failure_reason_instead_of_untested(self):
        for include_success in (False, True):
            with self.subTest(include_success=include_success):
                failure = error.HTTPError("http://localhost/delay", 504, "Gateway Timeout", {},
                                          io.BytesIO(b'{"message": "proxy dial failed"}'))
                responses = {"bad": failure}
                if include_success:
                    responses["ok"] = {"delay": 42}
                frames = self.run_latency_ui(responses, [ord("R"), -1])
                completed = next(frame for frame in frames if "测试完成" in frame[1])
                self.assertIn("1 个节点失败", completed[1])
                if include_success:
                    self.assertTrue(any("42 ms" in line for line in completed.values()))
                failed_row = next(line for line in completed.values() if "bad" in line)
                self.assertNotIn("未测", failed_row)
                self.assertIn("失败", failed_row)
                self.assertIn("proxy dial failed", failed_row)

    def test_all_nodes_keeps_transport_and_invalid_response_errors(self):
        responses = {"network": error.URLError("connection refused"),
                     "timeout": TimeoutError("timed out"),
                     "invalid": {"delay": None}, "zero": {"delay": 0},
                     "message": {"message": "TLS handshake failed"}}
        frames = self.run_latency_ui(responses, [ord("R"), -1])
        completed = next(frame for frame in frames if "测试完成" in frame[1])
        self.assertIn("4 个节点失败", completed[1])
        self.assertTrue(any("zero" in line and "0 ms" in line for line in completed.values()))
        for name, reason in (("network", "connection refused"), ("timeout", "timed out"),
                             ("invalid", "API 未返回有效延迟"),
                             ("message", "TLS handshake failed")):
            with self.subTest(name=name):
                row = next(line for line in completed.values() if name in line)
                self.assertIn("失败", row)
                self.assertIn(reason, row)

    def test_all_nodes_keeps_http_status_without_json_error_body(self):
        for body in (b"", b"not JSON", b"[]", b'{"message": null}'):
            with self.subTest(body=body):
                failure = error.HTTPError("http://localhost/delay", 503, "Service Unavailable", {},
                                          io.BytesIO(body))
                frames = self.run_latency_ui({"bad": failure}, [ord("R"), -1])
                completed = next(frame for frame in frames if "测试完成" in frame[1])
                row = next(line for line in completed.values() if "bad" in line)
                self.assertIn("503", row)
                self.assertIn("Service Unavailable", row)

    def test_retesting_replaces_old_delay_and_then_clears_failure(self):
        frames = self.run_latency_ui(
            {"bad": [{"delay": 42}, error.URLError("connection refused"), {"delay": 12}]},
            [ord("R"), -1, ord("R"), -1, ord("R"), -1])
        self.assertIn("未测", frames[0][3])
        failed = next(frame for frame in frames if "1 个节点失败" in frame[1])
        self.assertIn("connection refused", failed[3])
        self.assertNotIn("42 ms", failed[3])
        completed = [frame for frame in frames if "测试完成" in frame[1]][-1]
        self.assertIn("12 ms", completed[3])
        self.assertNotIn("失败", completed[3])

    def test_single_node_failure_shows_reason_in_row_and_footer(self):
        frames = self.run_latency_ui({"bad": error.URLError("connection refused")},
                                     [ord("j"), ord("r"), -1])
        completed = next(frame for frame in frames if "测试完成" in frame[1])
        self.assertIn("connection refused", completed[3])
        self.assertIn("connection refused", completed[22])

    def test_selected_failure_reason_is_visible_with_long_node_name(self):
        frames = self.run_latency_ui({"节点" * 90: error.URLError("connection refused")},
                                     [ord("j"), ord("R"), -1])
        completed = next(frame for frame in frames if "测试完成" in frame[1])
        self.assertIn("connection refused", completed[22])

    def test_failed_nodes_sort_after_successful_nodes(self):
        frames = self.run_latency_ui({"bad": error.URLError("connection refused"),
                                     "slow": {"delay": 42}, "fast": {"delay": 12}},
                                    [ord("R"), -1])
        completed = next(frame for frame in frames if "测试完成" in frame[1])
        for y, name in enumerate(("fast", "slow", "bad"), 3):
            self.assertIn(name, completed[y])

    def resolve_generated_release(self, digest, suffix=None, checksum_text=None):
        assets = [{"name": self.PACKAGE, "browser_download_url": self.DOWNLOAD_URL, "digest": digest}]
        checksum_url = "https://example.invalid/checksum"
        if suffix:
            assets.append({"name": self.PACKAGE + suffix, "browser_download_url": checksum_url})

        def respond(req, timeout):
            if req.full_url == "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest":
                return io.BytesIO(json.dumps({"assets": assets}).encode())
            self.assertEqual(req.full_url, checksum_url)
            return io.BytesIO(checksum_text.encode())

        script = module.root_script("user", "", "")
        resolver = script.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        output = io.StringIO()
        with patch("platform.machine", return_value="x86_64"), \
                patch("urllib.request.urlopen", side_effect=respond) as urlopen, redirect_stdout(output):
            exec(compile(resolver, "<bootstrap release resolver>", "exec"), {})
        return output.getvalue().splitlines(), urlopen.call_count

    def test_generated_bootstrap_resolves_github_digest(self):
        lines, calls = self.resolve_generated_release("sha256:" + self.SHA256)
        self.assertEqual(lines, [self.DOWNLOAD_URL, self.SHA256])
        self.assertEqual(calls, 1)

    def test_generated_bootstrap_prefers_digest_over_checksum_file(self):
        lines, calls = self.resolve_generated_release("sha256:" + self.SHA256, ".sha256", "invalid")
        self.assertEqual(lines, [self.DOWNLOAD_URL, self.SHA256])
        self.assertEqual(calls, 1)

    def test_generated_bootstrap_falls_back_to_checksum_file(self):
        for suffix in (".sha256", ".sha256sum"):
            for digest in (None, "", "sha256:invalid", "sha512:" + "a" * 128):
                with self.subTest(suffix=suffix, digest=digest):
                    lines, calls = self.resolve_generated_release(digest, suffix, self.SHA256 + "  " + self.PACKAGE + "\n")
                    self.assertEqual(lines, [self.DOWNLOAD_URL, self.SHA256])
                    self.assertEqual(calls, 2)

    def test_generated_bootstrap_rejects_missing_or_invalid_digest(self):
        for digest in (None, "", "sha256:" + "a" * 63, "sha256:" + "g" * 64,
                       "sha256:" + "a" * 65, "sha512:" + "a" * 128, {"sha256": self.SHA256}):
            with self.subTest(digest=digest), self.assertRaisesRegex(SystemExit, "SHA-256"):
                self.resolve_generated_release(digest)

    def test_generated_bootstrap_rejects_invalid_checksum_file(self):
        for checksum in ("", "\n", "a" * 63, "g" * 64, "a" * 65):
            with self.subTest(checksum=checksum), self.assertRaisesRegex(SystemExit, "SHA-256"):
                self.resolve_generated_release(None, ".sha256", checksum)

    def test_shell_output_is_safe_for_special_characters(self):
        value = "http://example.invalid/a'$(printf REVIEW_MARKER)"
        script = module.shell_env(True, value) + '\nprintf "%s" "$http_proxy"'
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, value)

    def test_unsafe_subscription_names_do_not_collide(self):
        self.assertNotEqual(module.profile_file("a/b"), module.profile_file("a?b"))

    def test_generated_bootstrap_has_valid_shell_syntax(self):
        script = module.root_script("user", "https://example.invalid/m.gz", "https://example.invalid/s?token=FAKE", "a" * 64)
        result = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
