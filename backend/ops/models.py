from django.core.validators import MaxValueValidator
from django.db import models

# The return window published when ``return_window_days`` is unset: a POLICY
# constant a merchant edits in the admin, not deployment config.
DEFAULT_RETURN_WINDOW_DAYS = 30

# Policy ceiling on a publishable window, enforced on the FIELD with
# MaxValueValidator so the form refuses it identically on every backend.
MAX_RETURN_WINDOW_DAYS = 3650

# The published value that means RETURNS ARE CLOSED (0), named because the
# model, the returns gate and the admin text must agree on it.
CLOSED_RETURN_WINDOW_DAYS = 0

# The storefront language tag: a merchant-facing choice, mirrored from
# ``LANGUAGE_CODE`` but deliberately not imported from it.
DEFAULT_SITE_LOCALE = "en-us"


class SiteSettings(models.Model):
    """Singleton store configuration, edited in the admin and served to the frontend via GET /api/settings/."""

    # Blank is a real state ("no override"): the storefront keeps its env
    # fallback.
    name = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text=(
            "Store name shown by the storefront. Leave blank to use the "
            "frontend's configured name."
        ),
    )
    # Blank + null: no logo is valid, and NULL says so rather than an empty
    # string path needing special-casing on read.
    logo = models.ImageField(
        upload_to="branding/",
        blank=True,
        null=True,
        help_text="Store mark shown beside the name. Leave blank for none.",
    )
    locale = models.CharField(
        max_length=35,
        default=DEFAULT_SITE_LOCALE,
        help_text="BCP 47 language tag for the storefront, e.g. en-us.",
    )
    # Publishing the flag only; the storefront gating that acts on it lives
    # elsewhere.
    maintenance_mode = models.BooleanField(
        default=False,
        help_text="Publish the storefront maintenance flag.",
    )

    support_email = models.EmailField(blank=True, default="")
    support_phone = models.CharField(max_length=32, blank=True, default="")
    whatsapp_number = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="International digits only, no '+'. Leave blank to hide WhatsApp.",
    )
    whatsapp_message = models.CharField(
        max_length=200,
        blank=True,
        default="Hi! I have a question about a fragrance.",
        help_text="Pre-filled message for the WhatsApp deep link.",
    )
    instagram_url = models.URLField(blank=True, default="")
    # Three states: NULL = unset (resolves to ``DEFAULT_RETURN_WINDOW_DAYS``),
    # 0 = returns CLOSED, N > 0 = returns accepted for N days. NULL and 0 are
    # never folded together. The merchant text lives in the admin fieldset
    # description because editing ``help_text`` would write a migration.
    return_window_days = models.PositiveIntegerField(
        blank=True,
        null=True,
        default=None,
        validators=[MaxValueValidator(MAX_RETURN_WINDOW_DAYS)],
        help_text=(
            "Days a customer has to request a return, counted from delivery "
            "where the goods have arrived and from the order date otherwise. "
            f"Leave blank for the store default ({DEFAULT_RETURN_WINDOW_DAYS})."
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def resolved_return_window_days(self):
        """The window the eligibility gate enforces, in days.

        NULL resolves to :data:`DEFAULT_RETURN_WINDOW_DAYS`; a published 0 (``CLOSED_RETURN_WINDOW_DAYS``) is returned as 0 and deliberately not defaulted, so the gate's closed branch stays reachable and the storefront is told the truth.
        """
        if self.return_window_days is None:
            return DEFAULT_RETURN_WINDOW_DAYS
        return self.return_window_days

    def returns_closed(self):
        """Whether this store has published ``CLOSED_RETURN_WINDOW_DAYS`` (0), refusing every return request.

        Compares the raw column with ``==``, never truthiness: 0 is falsy, and a falsy check would also fire on NULL, which is the unset case.
        """
        return self.return_window_days == CLOSED_RETURN_WINDOW_DAYS

    def __str__(self):
        return "Site settings"
