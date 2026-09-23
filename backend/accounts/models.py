from django.conf import settings
from django.db import models

# SPEC-17-05 [R-17.9] user-facing MFA messages, shared by the login
# serializer (staff API) and the admin login form so both enforcement
# surfaces speak with one voice. They name no username/state beyond what
# the already-submitted correct password proved.
MFA_ENROLLMENT_REQUIRED = (
    "Multi-factor authentication is mandatory for this account. "
    "Enroll a device at /api/accounts/mfa/setup/ before signing in."
)
MFA_CODE_REQUIRED = "Enter your authentication code."
MFA_CODE_INVALID = "Invalid or expired authentication code."


class TOTPDevice(models.Model):
    """One RFC 6238 TOTP secret per user (SPEC-17-05, R-17.9).

    Enforcement gate: a device counts only when ``enabled`` AND
    ``confirmed_at`` is set — setup mints an unconfirmed, disabled row that
    must pass a valid-code confirm before it can ever satisfy a login.
    Disabling keeps the row (enrollment history) but the secret is dead for
    verification; re-enrollment overwrites it with a fresh secret.

    Secret exposure discipline: ``secret`` is returned by the setup
    endpoint exactly once (enrollment) and by nothing else — not this
    model's str, not the status endpoint, never logged.
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
    # RFC 6238 §5.2 replay guard: the counter of the last accepted code;
    # codes at or below it never verify again. None until first use.
    last_used_counter = models.BigIntegerField(null=True, blank=True)
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
