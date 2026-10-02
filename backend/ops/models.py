from django.core.validators import MaxValueValidator
from django.db import models

# [R-1.16] SPEC-1-B07d: the return window this store publishes, used when
# ``SiteSettings.return_window_days`` is left unset. It is a POLICY constant,
# not deployment config: a merchant editing it in the admin is editing the
# store's return policy, and the two page-density keys in config/settings.py
# stay env-driven because a page density is a property of the deployment.
#
# WHY 30, in two independent arguments rather than one intuition:
#
# 1. The ecommerce convention. Thirty days is the window general retail
#    publishes and customers arrive expecting - the FTC's three-day
#    cooling-off rule for online orders is a statutory FLOOR, not the market
#    norm, and a perfume store selling unsealed-adjacent goods cannot claim a
#    change-of-mind period at all. A shorter default under-serves the customer
#    and buys the store nothing; a longer one is a liability the merchant must
#    be able to opt into deliberately.
# 2. What this repo's order lifecycle implies. ``orders.state``'s
#    ``LIFECYCLE_SEQUENCE`` runs pending -> confirmed -> shipped -> delivered,
#    and the eligibility gate ADMITS an order on its money half alone
#    (``captured / unfulfilled``) - a paid order the merchant has not yet
#    dispatched. Any window measured from the order date that is shorter than
#    the store's own packing-plus-courier latency would therefore expire a
#    paying customer's right to ask BEFORE the parcel exists. Thirty days
#    outlives that latency with room to spare, so the window is never the
#    binding constraint on an order nobody has shipped.
#
# It is a module constant rather than an env key for the reason the whole
# feature is: the number is merchant policy, and policy the merchant cannot
# change without a redeploy is not a policy.
DEFAULT_RETURN_WINDOW_DAYS = 30

# The ceiling on what a merchant may publish: a POLICY bound, not a storage
# limit. It is worth saying what it is NOT, because an earlier version of this
# comment claimed otherwise and was wrong: ``PositiveIntegerField`` maps to
# Postgres ``integer`` (int4, max 2,147,483,647) and SQLite has no integer
# width at all, so every value above 3650 is as storable on this repo's
# backends as every value below it, and there is no representability boundary
# near this number to defend against.
#
# WHY TEN YEARS, as policy. Past a decade a return window stops being a window
# a merchant can honour: stock turns over and no store keeps order records
# legible that long, so the number is "no window" in all but name - which is
# exactly the mistake the field's own NULL-means-unset convention avoids for
# the default. Declaring the ceiling keeps the admin HONEST about that: a
# policy that cannot be operated is refused as an input rather than silently
# stored as a deadline nothing can meet, and 3650 sits far enough above any
# real policy that no merchant loses a window they could have published.
#
# ``MaxValueValidator`` enforces it in Python, which is the point of putting
# the bound on the FIELD rather than on the column: the form is one code path
# on every backend, whereas the column's own bounds are enforced by Postgres
# and ignored by SQLite.
MAX_RETURN_WINDOW_DAYS = 3650


class SiteSettings(models.Model):
    """Singleton store configuration, edited in the admin, served to the
    frontend via GET /api/settings/. Blank fields mean "channel hidden"."""

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
    # SPEC-1-B07d. NULL is "unset", not zero: the admin's blank-means-default
    # convention makes the policy explicit (this store publishes 30) and
    # distinct from a merchant who deliberately publishes 0. The lower bound is
    # supplied by PositiveIntegerField and the upper one by MAX_RETURN_WINDOW_DAYS
    # - see its comment - so both are refused in the FORM, one code path on
    # every backend, rather than left to a column whose own bounds Postgres
    # enforces and SQLite does not.
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

        The unset case resolves to :data:`DEFAULT_RETURN_WINDOW_DAYS` rather
        than raising or returning None, so every caller - the gate and the
        public ``/api/settings/`` disclosure alike - quotes the SAME number the
        merchant would read in the admin. Publishing the resolved value rather
        than the raw NULL also means the storefront never has to implement a
        second copy of the default.
        """
        if self.return_window_days is None:
            return DEFAULT_RETURN_WINDOW_DAYS
        return self.return_window_days

    def __str__(self):
        return "Site settings"
