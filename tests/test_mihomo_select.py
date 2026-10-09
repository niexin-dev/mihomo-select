from contextlib import redirect_stdout
import importlib.util
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("mihomo_select", ROOT / "mihomo_select.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MihomoSelectTest(unittest.TestCase):
    PACKAGE = "mihomo-linux-amd64-v3-v1.19.32.gz"
    DOWNLOAD_URL = "https://github.com/MetaCubeX/mihomo/releases/download/v1.19.32/" + PACKAGE
    SHA256 = "13239bf2a4e9f8e172ee3d3910a5543ac15b7a9e08d3bdefe1cdcfc3d95855ae"

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
