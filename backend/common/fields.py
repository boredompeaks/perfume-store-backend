"""Fernet-encrypted text fields.

The store keeps secrets whose whole value is that nobody can read them out of
the database: a TOTP shared secret is a second factor, so a dump or a replica
read that yields it turns that factor off. ``EncryptedCharField`` moves the
plaintext boundary to the ORM: the column holds a Fernet token, an instance
holds the plaintext, and a token this deployment cannot decrypt raises instead
of being handed back as if it were the value.

The key itself is resolved at boot in ``config.settings`` (``TOTP_ENCRYPTION_KEY``)
because that is where this project refuses an unconfigured production boot;
this module only consumes the resolved value.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

KEY_SETTING = "TOTP_ENCRYPTION_KEY"


class FieldDecryptionError(Exception):
    """A stored value that the configured key cannot decrypt.

    Raised instead of returning the stored token: a caller that receives
    ciphertext where it expects a plaintext secret would feed it to an
    authenticator comparison, an enrollment URI or a log line, and the failure
    would surface as a wrong code far away from the cause.
    """


@lru_cache(maxsize=8)
def _fernet_for(key):
    # Cached per key VALUE rather than once per process, so an
    # override_settings in a test gets the cipher its settings declare.
    return Fernet(key)


def get_fernet():
    """The Fernet for ``settings.TOTP_ENCRYPTION_KEY``.

    The boot-time resolver fails closed outside development, so an empty or
    malformed key here means the setting was overridden after boot (a test, a
    plugin). It is refused rather than treated as "no encryption": a field
    that silently stores plaintext when its key is missing is the defect the
    field exists to prevent.
    """
    key = getattr(settings, KEY_SETTING, "")
    if not key:
        raise ImproperlyConfigured(
            f"{KEY_SETTING} is empty; field encryption cannot be silently "
            "skipped. Set a Fernet key (see backend/.env.example)."
        )
    try:
        return _fernet_for(key)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"{KEY_SETTING} is not a Fernet key: expected exactly 32 random "
            "bytes, urlsafe-base64 encoded."
        ) from exc


class EncryptedCharField(models.CharField):
    """A CharField whose column holds Fernet ciphertext, never the plaintext.

    Encryption runs in ``get_prep_value``, so every path that builds a SQL
    value passes through it (``save``, ``bulk_create``, ``QuerySet.update``);
    decryption runs in ``from_db_value``, so an instance read back carries the
    plaintext. Empty and None pass through untouched, which keeps "no secret
    yet" distinguishable from an encrypted one.

    Two consequences a caller has to live with:

    * The token is non-deterministic, so the field is not searchable by value:
      an exact lookup compares the fresh token built for the operand against
      the stored one and can never match.
    * ``max_length`` describes the CIPHERTEXT. A short secret still encrypts
      to roughly 100 base64 characters, and a token the column cannot hold is
      refused at the write rather than left to a backend-specific failure.

    Serialization (``dumpdata``, ``value_to_string``) reads the Python value,
    which is plaintext: the encryption boundary is the database column, not
    every representation of the object.
    """

    description = "Fernet-encrypted text"

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == "":
            return value
        token = get_fernet().encrypt(value.encode("utf-8")).decode("ascii")
        if self.max_length is not None and len(token) > self.max_length:
            raise ValueError(
                f"{self.name or 'This field'} encrypts to {len(token)} "
                f"characters but max_length is {self.max_length}; the "
                "ciphertext, not the plaintext, is what the column holds."
            )
        return token

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        try:
            return get_fernet().decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise FieldDecryptionError(
                f"{self.name or 'Encrypted field'} holds a value that cannot "
                f"be decrypted with the configured {KEY_SETTING}: it was "
                "written with a different key or the column was corrupted. "
                "The stored value was not returned."
            ) from exc
