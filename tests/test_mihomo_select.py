import importlib.util
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("mihomo_select", ROOT / "mihomo_select.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MihomoSelectTest(unittest.TestCase):
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
