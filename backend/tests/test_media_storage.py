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

_NON_DEBUG_ENV = {
    "DJANGO_SECRET_KEY": "x" * 50,
    "DJANGO_ENV": "production",
    "DATABASE_URL": "postgres://u:p@db.example.com:5432/perfume_store",
    "DJANGO_ALLOWED_HOSTS": "example.test",
    "CSRF_TRUSTED_ORIGINS": "https://example.test",
}

# Every S3 env var is declared EMPTY: load_dotenv() may have injected real values, and the builder treats empty like unset.
_S3_ENV_ABSENT = {
    "AWS_STORAGE_BUCKET_NAME": "",
    "AWS_S3_ENDPOINT_URL": "",
    "AWS_S3_REGION_NAME": "",
    "AWS_ACCESS_KEY_ID": "",
    "AWS_SECRET_ACCESS_KEY": "",
    "AWS_S3_CUSTOM_DOMAIN": "",
}

_LEAKED_ENV_NAMES = frozenset({"CSRF_TRUSTED_ORIGINS", "DATABASE_URL"})

_S3_ENV_KEYS = tuple(_S3_ENV_ABSENT)


def run_settings_import(
    env_overrides, snippet="import config.settings; print('IMPORT_OK')"
):
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
        cwd=tempfile.gettempdir(),
        env={**env, "PYTHONPATH": str(BACKEND_DIR)},
        timeout=60,
    )


class S3OptionsResolutionTests(SimpleTestCase):
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
        self.assertEqual(
            config_settings.STORAGES["default"]["BACKEND"],
            "django.core.files.storage.FileSystemStorage",
        )
        self.assertEqual(config_settings.STORAGES["default"]["OPTIONS"], {})

    def test_boot_resolves_storages_to_the_s3_backend_with_r2_endpoint(self):
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
        res = run_settings_import(
            {**_NON_DEBUG_ENV, "DJANGO_MEDIA_BACKEND": S3_BACKEND_PATH},
            snippet="import config.settings",
        )
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("AWS_STORAGE_BUCKET_NAME", res.stderr)

    def test_boot_without_any_s3_env_stays_on_the_filesystem(self):
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
    _BANNED_LITERALS = (
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
        fake_connection = mock.MagicMock()
        fake_connection.Bucket.return_value.name = "test-media-bucket"
        storage._connections.connection = fake_connection
        storage.exists = mock.MagicMock(return_value=False)
        return storage, fake_connection

    def test_save_stores_the_file_under_the_requested_key(self):
        storage, connection = self._storage()
        name = storage.save("products/eau-de-parfum.txt", ContentFile(b"upload-bytes"))
        self.assertEqual(name, "products/eau-de-parfum.txt")
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
