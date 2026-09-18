from django.db import models


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

    def __str__(self):
        return "Site settings"
