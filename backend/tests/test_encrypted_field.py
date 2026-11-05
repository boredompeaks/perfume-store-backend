"""PRISTINE-13d2a (#52 P0 prerequisite): encrypted field and key resolution.

The field is exercised through a REAL table, not by looking at what
``get_prep_value`` returned: a test-only model is built under an isolated app
registry (so no test that enumerates registered models can see it) and its
table is created with a schema editor, then written with ``save``,
``bulk_create`` and ``update`` and read back with a fresh query. The column is
the subject, which is why the raw-row assertions read it with a cursor.

Settings are import-time, so the boot decisions are pinned in a subprocess
with a scrubbed environment, the same pattern ``tests.test_settings_security``
uses for the DEBUG and DATABASE_URL guards.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, models
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.test.utils import isolate_apps

import config.settings as config_settings
from common.fields import EncryptedCharField, FieldDecryptionError

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Hand-written 32-byte keys (urlsafe base64 of b"A"*32 and b"B"*32), not
# generated: a key pin whose expected value came out of the code under test
# would agree with a wrong key from both sides.
KEY = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="
OTHER_KEY = "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI="

PLAINTEXT = "JBSWY3DPEHPK3PXP"

# A complete non-debug boot: the shape config.settings refuses to import when
# any part is missing (SPEC-22-03). TOTP_ENCRYPTION_KEY is deliberately absent
# from the base so each test adds it explicitly.
_NON_DEBUG_ENV = {
    "DJANGO_SECRET_KEY": "x" * 50,
    "DJANGO_ENV": "production",
    "DATABASE_URL": "postgres://u:p@db.example.com:5432/perfume_store",
    "DJANGO_ALLOWED_HOSTS": "example.test",
    "CSRF_TRUSTED_ORIGINS": "https://example.test",
}


def run_settings_boot(env_overrides, snippet="import config.settings; print('OK')"):
    """Import config.settings in a child with a scrubbed environment.

    Every DJANGO_* name and TOTP_ENCRYPTION_KEY is dropped rather than
    inherited: this process booted with the developer's (or the lane's)
    configuration, which is exactly what these tests must not measure.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DJANGO_")
        and key not in ("TOTP_ENCRYPTION_KEY", "DATABASE_URL")
    }
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        cwd=tempfile.gettempdir(),  # neutral cwd: no .env is discovered
        env={**env, "PYTHONPATH": str(BACKEND_DIR)},
        timeout=120,
    )


def build_holder_model():
    """Build the test-only model under an isolated app registry.

    Defined inside the isolation context so the global registry is untouched;
    the class stays usable after the context exits because persistence reads
    ``_meta`` and the connection, not the registry.
    """
    with isolate_apps("common"):

        class Holder(models.Model):
            secret = EncryptedCharField(max_length=255, null=True, blank=True)

            class Meta:
                app_label = "common"
                db_table = "tests_encrypted_field_holder"

        return Holder


class KeyResolutionTests(SimpleTestCase):
    """The boot-time resolver, measured in-process (every branch is a value)."""

    def test_development_key_is_deterministic_and_secret_specific(self):
        first = config_settings._derive_development_totp_key("secret-a")
        self.assertEqual(
            first, config_settings._derive_development_totp_key("secret-a")
        )
        self.assertNotEqual(
            first, config_settings._derive_development_totp_key("secret-b")
        )
        # It is a REAL Fernet key (32 bytes behind the base64), not merely a
        # deterministic string that happens to compare equal.
        Fernet(first)

    def test_an_explicit_key_is_returned_unchanged(self):
        resolved = config_settings._totp_encryption_key(
            KEY, "unused", debug=False, test_run=False
        )
        self.assertEqual(resolved, KEY)

    def test_a_missing_key_outside_development_refuses_to_boot(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            config_settings._totp_encryption_key(
                None, "secret", debug=False, test_run=False
            )
        self.assertIn("TOTP_ENCRYPTION_KEY", str(caught.exception))

    def test_a_blank_key_counts_as_missing(self):
        with self.assertRaises(ImproperlyConfigured):
            config_settings._totp_encryption_key(
                "   ", "secret", debug=False, test_run=False
            )

    def test_a_malformed_key_is_refused_even_in_development(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            config_settings._totp_encryption_key(
                "not-a-fernet-key", "secret", debug=True, test_run=True
            )
        self.assertIn("TOTP_ENCRYPTION_KEY", str(caught.exception))

    def test_development_derives_the_key_and_warns_once(self):
        with self.assertLogs("config.settings", level="WARNING") as logs:
            key = config_settings._totp_encryption_key(
                "", "dev-secret", debug=True, test_run=False
            )
        self.assertEqual(
            key, config_settings._derive_development_totp_key("dev-secret")
        )
        self.assertEqual(len(logs.records), 1)
        self.assertIn("TOTP_ENCRYPTION_KEY", logs.records[0].getMessage())

    def test_a_test_run_without_debug_may_derive_too(self):
        with self.assertLogs("config.settings", level="WARNING"):
            key = config_settings._totp_encryption_key(
                None, "dev-secret", debug=False, test_run=True
            )
        self.assertEqual(
            key, config_settings._derive_development_totp_key("dev-secret")
        )

    def test_an_empty_secret_key_cannot_derive(self):
        with self.assertRaises(ImproperlyConfigured):
            config_settings._derive_development_totp_key("")
        with self.assertLogs("config.settings", level="WARNING"):
            with self.assertRaises(ImproperlyConfigured):
                config_settings._totp_encryption_key("", "", debug=True, test_run=True)

    def test_the_booted_setting_is_a_valid_fernet_key(self):
        Fernet(settings.TOTP_ENCRYPTION_KEY)


class SettingsBootTests(SimpleTestCase):
    """The resolver is wired into the boot, not merely available beside it."""

    def test_a_non_debug_boot_without_the_key_is_refused(self):
        res = run_settings_boot(dict(_NON_DEBUG_ENV))
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("TOTP_ENCRYPTION_KEY", res.stderr)

    def test_a_non_debug_boot_resolves_a_supplied_key(self):
        res = run_settings_boot(
            {**_NON_DEBUG_ENV, "TOTP_ENCRYPTION_KEY": KEY},
            snippet=(
                "import config.settings as s; "
                "print('KEY', s.TOTP_ENCRYPTION_KEY == %r)" % KEY
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("KEY True", res.stdout)

    def test_a_test_run_without_the_key_derives_instead_of_refusing(self):
        res = run_settings_boot(
            dict(_NON_DEBUG_ENV),
            snippet=(
                "import sys; sys.argv = ['manage.py', 'test']; "
                "import config.settings as s; print('KEY', s.TOTP_ENCRYPTION_KEY)"
            ),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        expected = config_settings._derive_development_totp_key("x" * 50)
        self.assertIn(f"KEY {expected}", res.stdout)


class EncryptedFieldContractTests(SimpleTestCase):
    """The field's own edges, without a database."""

    def test_empty_and_none_pass_through_both_directions(self):
        field = EncryptedCharField(max_length=255)
        self.assertIsNone(field.get_prep_value(None))
        self.assertEqual(field.get_prep_value(""), "")
        self.assertIsNone(field.from_db_value(None, None, None))
        self.assertEqual(field.from_db_value("", None, None), "")

    def test_prep_and_decode_are_inverses(self):
        field = EncryptedCharField(max_length=255)
        token = field.get_prep_value(PLAINTEXT)
        self.assertNotEqual(token, PLAINTEXT)
        self.assertEqual(field.from_db_value(token, None, None), PLAINTEXT)

    def test_ciphertext_that_cannot_fit_the_column_is_refused_at_the_write(self):
        field = EncryptedCharField(max_length=40)
        with self.assertRaises(ValueError) as caught:
            field.get_prep_value(PLAINTEXT)
        self.assertIn("max_length", str(caught.exception))

    def test_an_unconfigured_key_refuses_the_write(self):
        field = EncryptedCharField(max_length=255)
        with override_settings(TOTP_ENCRYPTION_KEY=""):
            with self.assertRaises(ImproperlyConfigured) as caught:
                field.get_prep_value(PLAINTEXT)
        self.assertIn("TOTP_ENCRYPTION_KEY", str(caught.exception))

    def test_a_malformed_configured_key_refuses_the_write(self):
        field = EncryptedCharField(max_length=255)
        with override_settings(TOTP_ENCRYPTION_KEY="garbage"):
            with self.assertRaises(ImproperlyConfigured):
                field.get_prep_value(PLAINTEXT)


class EncryptedFieldDatabaseTests(TransactionTestCase):
    """Round trips through a real table on whichever engine is under test."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.holder = build_holder_model()
        with connection.schema_editor() as editor:
            editor.create_model(cls.holder)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            editor.delete_model(cls.holder)
        super().tearDownClass()

    def raw_column(self, pk):
        table = connection.ops.quote_name(self.holder._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT secret FROM {table} WHERE id = %s", [pk])
            return cursor.fetchone()[0]

    def write_raw(self, pk, value):
        table = connection.ops.quote_name(self.holder._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {table} SET secret = %s WHERE id = %s", [value, pk])

    def test_round_trip_through_the_database_returns_plaintext(self):
        row = self.holder.objects.create(secret=PLAINTEXT)
        self.assertEqual(row.secret, PLAINTEXT)
        fetched = self.holder.objects.get(pk=row.pk)
        self.assertEqual(fetched.secret, PLAINTEXT)

    def test_the_column_holds_ciphertext_not_the_plaintext(self):
        row = self.holder.objects.create(secret=PLAINTEXT)
        stored = self.raw_column(row.pk)
        self.assertNotEqual(stored, PLAINTEXT)
        self.assertNotIn(PLAINTEXT, stored)
        # And it is a Fernet token under the booted key: not merely "some
        # other string", which a hash or a bug could also produce.
        self.assertEqual(
            Fernet(settings.TOTP_ENCRYPTION_KEY).decrypt(stored.encode()).decode(),
            PLAINTEXT,
        )

    def test_each_write_uses_a_fresh_token(self):
        rows = [self.holder.objects.create(secret=PLAINTEXT) for _ in range(2)]
        first, second = (self.raw_column(row.pk) for row in rows)
        self.assertNotEqual(first, second)
        for token in (first, second):
            self.assertEqual(
                Fernet(settings.TOTP_ENCRYPTION_KEY).decrypt(token.encode()).decode(),
                PLAINTEXT,
            )

    def test_empty_string_is_stored_verbatim(self):
        row = self.holder.objects.create(secret="")
        self.assertEqual(self.raw_column(row.pk), "")
        self.assertEqual(self.holder.objects.get(pk=row.pk).secret, "")

    def test_none_is_stored_as_null(self):
        row = self.holder.objects.create(secret=None)
        self.assertIsNone(self.raw_column(row.pk))
        self.assertIsNone(self.holder.objects.get(pk=row.pk).secret)

    def test_bulk_create_encrypts(self):
        [row] = self.holder.objects.bulk_create([self.holder(secret=PLAINTEXT)])
        self.assertNotEqual(self.raw_column(row.pk), PLAINTEXT)
        self.assertEqual(self.holder.objects.get(pk=row.pk).secret, PLAINTEXT)

    def test_queryset_update_encrypts(self):
        row = self.holder.objects.create(secret="before")
        self.holder.objects.filter(pk=row.pk).update(secret=PLAINTEXT)
        stored = self.raw_column(row.pk)
        self.assertNotEqual(stored, PLAINTEXT)
        self.assertEqual(self.holder.objects.get(pk=row.pk).secret, PLAINTEXT)

    def test_corrupt_token_raises_the_typed_error(self):
        row = self.holder.objects.create(secret=PLAINTEXT)
        self.write_raw(row.pk, "not-a-fernet-token")
        with self.assertRaises(FieldDecryptionError):
            self.holder.objects.get(pk=row.pk)

    def test_a_value_written_under_another_key_raises_the_typed_error(self):
        row = self.holder.objects.create(secret=PLAINTEXT)
        with override_settings(TOTP_ENCRYPTION_KEY=OTHER_KEY):
            with self.assertRaises(FieldDecryptionError):
                self.holder.objects.get(pk=row.pk)

    def test_a_plaintext_left_in_the_column_is_refused_not_returned(self):
        # The pre-encryption shape: an old row (or a hand-written one) whose
        # column still holds the secret. It must not read back as the value.
        row = self.holder.objects.create(secret=PLAINTEXT)
        self.write_raw(row.pk, PLAINTEXT)
        with self.assertRaises(FieldDecryptionError):
            self.holder.objects.get(pk=row.pk)

    def test_a_non_ascii_column_value_raises_the_typed_error(self):
        row = self.holder.objects.create(secret=PLAINTEXT)
        self.write_raw(row.pk, "s\u00e9cret")
        with self.assertRaises(FieldDecryptionError):
            self.holder.objects.get(pk=row.pk)
