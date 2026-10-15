"""TIER1-S3-MEDIA: the media-storage seam wired to real S3-compatible
object storage (Cloudflare R2 preferred, AWS S3 alternative - one
implementation, django-storages' S3Storage).

The pins, each in its own class below:
  (a) STORAGES resolves from env to the S3 backend class with
      endpoint_url honored (R2 case) and absent (AWS case);
  (b) no bucket name, credential or endpoint literal in the settings
      source or in .env.example (white-label product);
  (c) the upload path drives the real storage API against a MOCKED boto3
      connection - the same no-network rule the Razorpay client lives by;
  (d) with the S3 env vars unset, the local-dev default (FileSystemStorage)
      is untouched, so the seam cannot brick a development checkout.

Settings are import-time, so the full env -> STORAGES resolution runs in a
subprocess against a clean environment (the pattern
tests/test_settings_security.py establishes); the pure builder is
additionally driven in-process under a patched environment.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile
from django.test import SimpleTestCase
from storages.backends.s3 import S3Storage

import config.settings as config_settings

BACKEND_DIR = Path(__file__).resolve().parent.parent

S3_BACKEND_PATH = "storages.backends.s3.S3Storage"

# A complete non-debug boot - the production shape the settings module
# accepts unmodified (same values as tests/test_settings_security.py).
_NON_DEBUG_ENV = {
    "DJANGO_SECRET_KEY": "x" * 50,
    "DJANGO_ENV": "production",
    "DATABASE_URL": "postgres://u:p@db.example.com:5432/perfume_store",
    "DJANGO_ALLOWED_HOSTS": "example.test",
    "CSRF_TRUSTED_ORIGINS": "https://example.test",
}

# Every S3 env var the builder reads, declared EMPTY: config.settings
# load_dotenv()s at import, so a git-ignored backend/.env may have injected
# real values into this process, and each test below pins the behaviour of
# an ABSENT variable by overriding it with the empty string (which the
# builder treats exactly like unset). A test composes this with the keys it
# wants present.
_S3_ENV_ABSENT = {
    "AWS_STORAGE_BUCKET_NAME": "",
    "AWS_S3_ENDPOINT_URL": "",
    "AWS_S3_REGION_NAME": "",
    "AWS_ACCESS_KEY_ID": "",
    "AWS_SECRET_ACCESS_KEY": "",
    "AWS_S3_CUSTOM_DOMAIN": "",
}

# Env names this module pins that the DJANGO_/AWS_ prefix scrub below does
# not already catch (they are read by config.settings but named without
# either family prefix).
_LEAKED_ENV_NAMES = frozenset({"CSRF_TRUSTED_ORIGINS", "DATABASE_URL"})

# The six keys the S3 block documents; the hygiene test pins that
# .env.example documents each one EMPTY.
_S3_ENV_KEYS = tuple(_S3_ENV_ABSENT)


def run_settings_import(
    env_overrides, snippet="import config.settings; print('IMPORT_OK')"
):
    """Boot config.settings in a subprocess under env_overrides.

    Copied from the settings-security module's hermetic pattern: strip the
    DJANGO_/RAZORPAY_/EMAIL_/AWS_ families (plus a short name list) so an
    untracked local .env cannot leak into the supposedly clean child, run
    from a neutral cwd so no .env is discovered, and put the backend on
    PYTHONPATH so `import config.settings` resolves.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DJANGO_", "RAZORPAY_", "EMAIL_", "AWS_"))
        and k not in _LEAKED_ENV_NAMES
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


class S3OptionsResolutionTests(SimpleTestCase):
    """Pin (a) + (d): the pure builder, driven in-process with a patched
    environment, plus the untouched local-dev default it must not disturb."""

    def _options_with(self, **env):
        with mock.patch.dict(os.environ, env, clear=False):
            return config_settings._env_media_storage_options(S3_BACKEND_PATH)

    def test_r2_case_honours_the_endpoint_url(self):
        options = self._options_with(
            **{
                **_S3_ENV_ABSENT,
                "AWS_STORAGE_BUCKET_NAME": "test-media-bucket",
                "AWS_S3_ENDPOINT_URL": "https://objects.example.test",
                "AWS_S3_REGION_NAME": "auto",
                "AWS_ACCESS_KEY_ID": "test-access-key",
                "AWS_SECRET_ACCESS_KEY": "test-secret-key",
            }
        )
        self.assertEqual(
            options,
            {
                "bucket_name": "test-media-bucket",
                "file_overwrite": False,
                "endpoint_url": "https://objects.example.test",
                "region_name": "auto",
                "access_key": "test-access-key",
                "secret_key": "test-secret-key",
            },
        )

    def test_aws_case_omits_the_endpoint_url(self):
        # Plain AWS S3: bucket, region and a credential, NO custom
        # endpoint - boto3 then talks to Amazon's own endpoints.
        options = self._options_with(
            **{
                **_S3_ENV_ABSENT,
                "AWS_STORAGE_BUCKET_NAME": "test-media-bucket",
                "AWS_S3_REGION_NAME": "us-east-1",
                "AWS_ACCESS_KEY_ID": "test-access-key",
                "AWS_SECRET_ACCESS_KEY": "test-secret-key",
            }
        )
        self.assertNotIn("endpoint_url", options)
        self.assertEqual(options["bucket_name"], "test-media-bucket")
        self.assertEqual(options["region_name"], "us-east-1")

    def test_custom_domain_is_passed_through_when_set(self):
        options = self._options_with(
            **{
                **_S3_ENV_ABSENT,
                "AWS_STORAGE_BUCKET_NAME": "test-media-bucket",
                "AWS_S3_CUSTOM_DOMAIN": "media.example.test",
            }
        )
        self.assertEqual(options["custom_domain"], "media.example.test")

    def test_non_s3_backends_get_no_options(self):
        # The builder only speaks when the S3 backend is the selected one;
        # every other backend (the filesystem default, a future third
        # party) keeps the empty OPTIONS Django ships with.
        self.assertEqual(
            config_settings._env_media_storage_options(
                "django.core.files.storage.FileSystemStorage"
            ),
            {},
        )
        self.assertEqual(
            config_settings._env_media_storage_options("example.bucket.MediaBucket"),
            {},
        )

    def test_s3_selection_without_a_bucket_refuses_at_boot(self):
        with mock.patch.dict(os.environ, {**_S3_ENV_ABSENT}, clear=False):
            with self.assertRaises(ImproperlyConfigured) as caught:
                config_settings._env_media_storage_options(S3_BACKEND_PATH)
        self.assertIn("AWS_STORAGE_BUCKET_NAME", str(caught.exception))

    def test_local_dev_default_is_untouched_without_any_s3_env(self):
        # Pin (d), in-process: this test process booted with no S3 env, so
        # the module-level STORAGES is the documented filesystem default
        # with empty OPTIONS - the seam cannot brick an unconfigured dev
        # checkout.
        self.assertEqual(
            config_settings.STORAGES["default"]["BACKEND"],
            "django.core.files.storage.FileSystemStorage",
        )
        self.assertEqual(config_settings.STORAGES["default"]["OPTIONS"], {})

    def test_boot_resolves_storages_to_the_s3_backend_with_r2_endpoint(self):
        # Pin (a), full-path: a clean-env subprocess boot with the R2 shape
        # (bucket + custom endpoint) resolves STORAGES to the S3 class and
        # carries the endpoint through.
        res = run_settings_import(
            {
                **_NON_DEBUG_ENV,
                "DJANGO_MEDIA_BACKEND": S3_BACKEND_PATH,
                "AWS_STORAGE_BUCKET_NAME": "test-media-bucket",
                "AWS_S3_ENDPOINT_URL": "https://objects.example.test",
            },
            snippet=(
                "import config.settings as s; "
                "print('DEFAULT', s.STORAGES['default']['BACKEND']); "
                "print('OPTIONS', s.STORAGES['default']['OPTIONS'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DEFAULT storages.backends.s3.S3Storage", res.stdout)
        self.assertIn("'bucket_name': 'test-media-bucket'", res.stdout)
        self.assertIn("'endpoint_url': 'https://objects.example.test'", res.stdout)

    def test_boot_aws_case_omits_the_endpoint_url(self):
        # Pin (a), full-path: the same boot WITHOUT a custom endpoint
        # resolves to the S3 class and carries no endpoint_url key.
        res = run_settings_import(
            {
                **_NON_DEBUG_ENV,
                "DJANGO_MEDIA_BACKEND": S3_BACKEND_PATH,
                "AWS_STORAGE_BUCKET_NAME": "test-media-bucket",
            },
            snippet=(
                "import config.settings as s; "
                "print('DEFAULT', s.STORAGES['default']['BACKEND']); "
                "print('OPTIONS', s.STORAGES['default']['OPTIONS'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DEFAULT storages.backends.s3.S3Storage", res.stdout)
        self.assertIn("'bucket_name': 'test-media-bucket'", res.stdout)
        self.assertNotIn("endpoint_url", res.stdout)

    def test_s3_selection_without_bucket_refuses_to_boot(self):
        # The fail-early half of (a): S3 selected, bucket missing - the
        # subprocess exits non-zero and the message names the key.
        res = run_settings_import(
            {**_NON_DEBUG_ENV, "DJANGO_MEDIA_BACKEND": S3_BACKEND_PATH},
            snippet="import config.settings",
        )
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("AWS_STORAGE_BUCKET_NAME", res.stderr)

    def test_boot_without_any_s3_env_stays_on_the_filesystem(self):
        # Pin (d), full-path: the same clean boot with NO S3 env at all
        # resolves to the filesystem default with empty OPTIONS.
        res = run_settings_import(
            dict(_NON_DEBUG_ENV),
            snippet=(
                "import config.settings as s; "
                "print('DEFAULT', s.STORAGES['default']['BACKEND']); "
                "print('OPTIONS', s.STORAGES['default']['OPTIONS'])"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DEFAULT django.core.files.storage.FileSystemStorage", res.stdout)
        self.assertIn("OPTIONS {}", res.stdout)


class SettingsSourceHygieneTests(SimpleTestCase):
    """Pin (b): white-label - no tenant value may live in the settings
    source or in the documented keys' values."""

    _BANNED_LITERALS = (
        # Provider endpoint fragments and the AWS key prefix: a baked
        # tenant config can only appear as one of these.
        "r2.cloudflarestorage.com",
        "amazonaws.com",
        "AKIA",
    )

    def test_settings_source_carries_no_tenant_literal(self):
        source = (BACKEND_DIR / "config" / "settings.py").read_text(encoding="utf-8")
        for banned in self._BANNED_LITERALS:
            self.assertNotIn(banned, source)

    def test_env_example_carries_no_tenant_literal_and_empty_s3_keys(self):
        env_example = (BACKEND_DIR / ".env.example").read_text(encoding="utf-8")
        for banned in self._BANNED_LITERALS:
            self.assertNotIn(banned, env_example)
        # Every S3 key is documented, and documented EMPTY: a uncommented
        # non-empty value here would be a committed default credential.
        for line in env_example.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for key in _S3_ENV_KEYS:
                if stripped.startswith(key + "="):
                    self.assertEqual(
                        stripped[len(key) + 1 :],
                        "",
                        f"{key} must be documented empty, got {stripped!r}",
                    )


class S3UploadAgainstMockedClientTests(SimpleTestCase):
    """Pin (c): the real S3Storage save()/url() path with the boto3
    connection faked - the same no-network rule as Razorpay. The storage is
    built from the settings builder's own output (production shape), so the
    test exercises the seam end to end minus one network hop."""

    def _storage(self):
        with mock.patch.dict(
            os.environ,
            {
                **_S3_ENV_ABSENT,
                "AWS_STORAGE_BUCKET_NAME": "test-media-bucket",
                "AWS_S3_ENDPOINT_URL": "https://objects.example.test",
                "AWS_ACCESS_KEY_ID": "test-access-key",
                "AWS_SECRET_ACCESS_KEY": "test-secret-key",
            },
            clear=False,
        ):
            options = config_settings._env_media_storage_options(S3_BACKEND_PATH)
        storage = S3Storage(**options)
        # Shadow the connection with a fake: save() then reaches the bucket
        # through storage.connection.Bucket(...).Object(...).upload_fileobj
        # and url() through connection.meta.client.generate_presigned_url,
        # with no socket ever opened.
        fake_connection = mock.MagicMock()
        fake_connection.Bucket.return_value.name = "test-media-bucket"
        storage._connections.connection = fake_connection
        # exists() is a head_object round trip on real S3; the name is new.
        storage.exists = mock.MagicMock(return_value=False)
        return storage, fake_connection

    def test_save_stores_the_file_under_the_requested_key(self):
        storage, connection = self._storage()
        name = storage.save("products/eau-de-parfum.txt", ContentFile(b"upload-bytes"))
        self.assertEqual(name, "products/eau-de-parfum.txt")
        # The endpoint the builder produced is honoured by the instance.
        self.assertEqual(storage.endpoint_url, "https://objects.example.test")
        connection.Bucket.assert_called_once_with("test-media-bucket")
        connection.Bucket.return_value.Object.assert_called_once_with(
            "products/eau-de-parfum.txt"
        )
        upload = connection.Bucket.return_value.Object.return_value.upload_fileobj
        upload.assert_called_once()
        args, kwargs = upload.call_args
        self.assertEqual(args[0].read(), b"upload-bytes")
        self.assertEqual(kwargs["ExtraArgs"]["ContentType"], "text/plain")

    def test_url_is_generated_through_the_mocked_connection(self):
        storage, connection = self._storage()
        connection.meta.client.generate_presigned_url.return_value = (
            "https://objects.example.test/test-media-bucket/products/x?sig=1"
        )
        url = storage.url("products/x")
        self.assertEqual(
            url, "https://objects.example.test/test-media-bucket/products/x?sig=1"
        )
        call = connection.meta.client.generate_presigned_url.call_args
        self.assertEqual(call.args[0], "get_object")
        self.assertEqual(call.kwargs["Params"]["Bucket"], "test-media-bucket")
        self.assertEqual(call.kwargs["Params"]["Key"], "products/x")
