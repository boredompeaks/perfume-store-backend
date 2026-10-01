"""Shipping methods and their rates (SPEC-1-B05 [R-1.07], spec 6.9).

Spec 6.9 names two entities and they answer two different questions, so
they are two tables: a shipping METHOD is what a customer picks ("Standard",
"Express") and a RATE is what that method costs for one geography ("Zones,
rates, carriers, restrictions", line 4155). A method therefore carries many
rates and the geography lives on the rate -- a national method is a method
with one wildcard rate, not a method with no geography at all.

Weight-based rates (spec 6.9 line 2003 names them beside flat rates) are
deliberately NOT modelled here, and the reason is a fact about the catalogue
rather than a scope decision: `products.products` carries `size` (the bottle
volume) and no weight in grams anywhere, so a weight band would have nothing
to match against. It belongs to the first task that gives a product a weight
to weigh.
"""

from django.db import models


class ShippingMethod(models.Model):
    """A delivery option a customer can pick, priced by its rates.

    ``code`` is the client-facing IDENTIFIER (spec 9.1's "Select delivery
    option"): checkout accepts this value and prices it server-side, never an
    amount, so the slug is part of the money path's trust boundary. It is
    staff-entered rather than generated, so there is no slug-generation race
    to retry (conventions.md:17 is about generation); the unique index plus
    the ModelForm's own uniqueness validation is what keeps two methods from
    answering one code.
    """

    code = models.SlugField(
        max_length=40,
        unique=True,
    )

    name = models.CharField(
        max_length=100,
    )

    description = models.CharField(
        max_length=200,
        blank=True,
        default="",
    )

    # Retiring a method must not reprice or unhide historical orders, so
    # deactivation is the supported way to stop offering one. An order keeps
    # the amount it was charged whatever happens to this row afterwards.
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)

    def __str__(self):
        return self.name


class ShippingRate(models.Model):
    """What one method costs for one geography.

    A rate matches a destination when BOTH of its geography rules hold, and
    an empty rule is a wildcard: an empty ``region`` matches every region and
    an empty ``postal_code_prefix`` matches every postal code. That is what
    makes a national rate expressible without a special case.

    ``postal_code_prefix`` is the spec's "Serviceable postal codes/regions"
    (line 2001) and the storefront's "pincode checker" (line 657) in one
    column: matching is prefix-based, so "400" covers the 400xxx band
    without enumerating it.
    """

    method = models.ForeignKey(
        ShippingMethod,
        on_delete=models.CASCADE,
        related_name="rates",
    )

    # conventions.md:15 - DecimalField(max_digits=10, decimal_places=2), the
    # same shape as every other money column in this repo. Never a float:
    # the amount is added to an order total, so a float here would be a
    # rounding error in the customer's favour at the database's expense.
    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
    )

    region = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Region (state) this rate serves. Empty matches any region.",
    )

    postal_code_prefix = models.CharField(
        max_length=10,
        blank=True,
        default="",
        help_text=("Postal-code prefix this rate serves. Empty matches any code."),
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("amount", "pk")
        constraints = [
            # One amount per method per geography. Without this, a store
            # could hold two rates that both match one destination and the
            # winner would be a coin toss the admin cannot see; with it, the
            # admin form says so at the point of entry. Specificity and
            # price still decide BETWEEN different geographies.
            models.UniqueConstraint(
                fields=["method", "region", "postal_code_prefix"],
                name="shipping_rate_geography_uidx",
            ),
        ]

    def __str__(self):
        geography = self.region or "anywhere"
        if self.postal_code_prefix:
            geography = f"{geography} {self.postal_code_prefix}*"
        return f"{self.method.code} {geography} {self.amount}"
