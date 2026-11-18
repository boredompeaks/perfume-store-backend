from django.conf import settings
from django.db import models

# User-facing MFA messages, shared by the staff API and the admin login form so
# both enforcement surfaces speak with one voice.
MFA_ENROLLMENT_REQUIRED = (
    "Multi-factor authentication is mandatory for this account. "
    "Enroll a device at /api/accounts/mfa/setup/ before signing in."
)
MFA_CODE_REQUIRED = "Enter your authentication code."
MFA_CODE_INVALID = "Invalid or expired authentication code."


class TOTPDevice(models.Model):
    """One RFC 6238 TOTP secret per user; it counts only when ``enabled`` and
    ``confirmed_at`` are set, and ``secret`` leaves the server once at enrollment.

    ``trusted_until`` is the explicit "trust this device" opt-in and never stands
    in for a code.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="totp_device",
    )
    # base32, unpadded (32 chars for the 20-byte secrets common.totp mints).
    secret = models.CharField(max_length=64)
    enabled = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # RFC 6238 §5.2 replay guard: codes at or below this counter never verify again.
    last_used_counter = models.BigIntegerField(null=True, blank=True)
    # NULL = never trusted = challenged every login.
    trusted_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "TOTP device"
        verbose_name_plural = "TOTP devices"

    def __str__(self):
        return f"TOTP device for {self.user}"

    @classmethod
    def active_for(cls, user):
        """The one verification-eligible device for ``user``, or None."""
        return cls.objects.filter(
            user=user, enabled=True, confirmed_at__isnull=False
        ).first()


class Address(models.Model):
    """A saved delivery address owned by exactly one customer.

    Every read/write is filtered by ``user``, so a foreign id is indistinguishable
    from a missing one (uniform 404); the per-user default is a view-level
    invariant maintained inside one transaction
    (``accounts.views._lock_user_addresses``).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="addresses",
    )
    label = models.CharField(max_length=40, blank=True)
    line1 = models.CharField(max_length=255)
    line2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20)
    country = models.CharField(max_length=100)
    phone = models.CharField(max_length=32, blank=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_default", "id"]

    def __str__(self):
        return f"{self.label or self.line1} ({self.user})"
