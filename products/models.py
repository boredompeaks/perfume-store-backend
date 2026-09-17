from django.db import models
from django.utils.text import slugify


class products(models.Model):
    name = models.CharField(max_length=100)

    slug = models.SlugField(
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    description = models.TextField()
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2
    )
    size = models.PositiveBigIntegerField()
    stock = models.PositiveBigIntegerField(default=0)
    category = models.CharField(max_length=50)
    image = models.ImageField(
        upload_to='products/',
        blank=True,
        null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):

        if not self.slug:
            base_slug = slugify(self.name) or 'product'
            slug = base_slug
            suffix = 2
            while products.objects.exclude(pk=self.pk).filter(slug=slug).exists():
                slug = f'{base_slug[:95]}-{suffix}'
                suffix += 1
            self.slug = slug

        super().save(*args, **kwargs)

    def __str__(self):
        return self.name
