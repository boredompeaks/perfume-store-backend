"""Settings security tests - V-02 (DEBUG fails open) and V-01 containment.

The DEBUG guard lives at settings-import time, so it is exercised in a
subprocess with a clean environment (no .env is loaded from a neutral cwd).
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

BACKEND_DIR = Path(__file__).resolve().parent.parent


def run_settings_import(env_overrides, snippet="import config.settings; print('IMPORT_OK')"):
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("DJANGO_", "RAZORPAY_", "EMAIL_"))
    }
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        cwd=tempfile.gettempdir(),  # neutral cwd: no .env is discovered
        env={**env, "PYTHONPATH": str(BACKEND_DIR)},
        timeout=60,
    )


class DebugDefaultTests(SimpleTestCase):
    def test_v02_debug_fails_open_currently(self):
        """V-02: a missing DJANGO_DEBUG env var currently enables DEBUG
        (fail-open). Pinned via subprocess because the test runner itself
        forces DEBUG=False in-process. Flip to assert False when the default
        flips to fail-closed."""
        res = run_settings_import(
            {},
            snippet="import config.settings as s; print('DEBUG_IS', s.DEBUG)",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DEBUG_IS True", res.stdout)


class SettingsGuardTests(SimpleTestCase):
    def test_v02_debug_false_without_secret_key_is_refused(self):
        res = run_settings_import({"DJANGO_DEBUG": "false"})
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("DJANGO_SECRET_KEY", res.stderr)

    def test_debug_false_with_secret_key_imports_cleanly(self):
        res = run_settings_import(
            {"DJANGO_DEBUG": "false", "DJANGO_SECRET_KEY": "x" * 50}
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("IMPORT_OK", res.stdout)

    def test_debug_true_works_without_secret_key(self):
        res = run_settings_import({"DJANGO_DEBUG": "true"})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("IMPORT_OK", res.stdout)
