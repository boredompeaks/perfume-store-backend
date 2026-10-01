"""Shipping methods, their rates, and the parcels that leave on them.

SPEC-1-B05 [R-1.07] owns the first half; SPEC-1-B06 [R-1.08] adds the
shipment half, in the same app because the spec treats them as one admin
domain (6.9, "Shipping and fulfilment") and B05's changelog records the
decision that B06 would land beside these two tables rather than open a
second app.

Shipping methods and their rates answer "what does delivery cost".
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

from django.conf import settings
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


class Shipment(models.Model):
    """[R-1.08] SPEC-1-B06: one parcel leaving the store for one order.

    Spec 6.9 names "Shipment creation" (line 2009), "Tracking number" (line
    2013) and "Carrier integration" (line 2007) as the shipping admin
    surface, and spec 10.2 gives the shipment its OWN lifecycle dimension --
    "Label created, dispatched, in transit, delivered, exception" (line
    3465). Those five words are the values of ``status`` verbatim; nothing
    here is an invented vocabulary, and in particular none of the deliberately
    vague words spec line 3487 warns against ("success", "done").

    One row per PARCEL, not per order: spec line 2021 asks for split
    shipments "if needed", and no order-level status could express two
    parcels leaving on different days, so ``order`` is one-to-many.

    Deliberately NOT wired into ``orders.state``. Spec 10.2 lists shipment as
    a dimension BESIDE fulfilment, and SPEC-1-B03 scoped that machine for
    audit; requirement 8 of this task forbids widening it. The two lifecycles
    stay separate records of separate facts, and the shipment vocabulary is
    never pushed through the order transition machine.
    """

    class Status(models.TextChoices):
        # Spec 10.2 line 3465, in the order the spec lists them.
        LABEL_CREATED = "label_created", "Label created"
        DISPATCHED = "dispatched", "Dispatched"
        IN_TRANSIT = "in_transit", "In transit"
        DELIVERED = "delivered", "Delivered"
        # "Exception" is the spec's own catch-all and the state spec 6.9 line
        # 2017 means by "Delivery failure handling".
        EXCEPTION = "exception", "Exception"

    order = models.ForeignKey(
        # A string reference, not an import: orders.models already points at
        # shipping.ShippingMethod, and importing it back would be a cycle.
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="shipments",
    )

    # The carrier reference a customer quotes to the carrier. UNIQUE because
    # one reference must never answer for two parcels: spec 8.3 line 2565
    # prescribes an index on the "Shipment tracking reference", and the
    # backing unique index is that index (same shape Order.order_number uses
    # for the spec's "order number" index). It is staff-entered, not
    # generated, so there is no generation race to retry.
    tracking_number = models.CharField(
        max_length=100,
        unique=True,
    )

    # The name the customer sees on the tracking page. Blank rather than
    # required: an unbranded courier still hands out a reference.
    carrier = models.CharField(
        max_length=60,
        blank=True,
        default="",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.LABEL_CREATED,
    )

    # Spec line 997's "Estimated delivery" on the confirmation page. NULL
    # until a carrier commits to a date, which is a fact about the carrier,
    # not a sentinel date.
    estimated_delivery = models.DateTimeField(
        null=True,
        blank=True,
    )

    # The staff-only half of the row: spec 6.9 line 2027 says the customer
    # tracking page must show neither "internal carrier credentials" nor
    # "warehouse notes", so both live here and this one field is NEVER on the
    # tracking serializer. One free-text field rather than two is what makes
    # that structural: there is no second place for an operator to type one.
    internal_note = models.TextField(
        blank=True,
        default="",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "shipment"
        verbose_name_plural = "shipments"

    def __str__(self):
        # No `order` traversal: this label is what the admin grid, the CSV
        # export and LogEntry render per row, and an order can be a guest row
        # whose own __str__ already has to be null-safe (SPEC-1-B04).
        return f"Shipment {self.tracking_number} ({self.status})"


class ShipmentEvent(models.Model):
    """[R-1.08] SPEC-1-B06: one recorded status change on one shipment.

    Spec 6.9 line 2015 names "Tracking events" as its own feature, and spec
    10.3 lists an "Audit record" as the last element of every transition's
    contract. This is that record, and it is the ONLY place a status change
    is remembered: the customer trail the tracking endpoint serves is this
    table, so a shipment's history cannot be reconstructed differently from
    the history an operator saw.

    Append-only, exactly as ``OrderStatusEvent`` is: the save guard below
    rejects any pk-set re-save and the admin registration is view-only, so no
    code path can rewrite a recorded step. ``from_status`` is NULL exactly on
    the creation event (a shipment that has never left a previous state).

    ``actor`` is SET_NULL for the same reason OrderStatusEvent.actor is:
    deleting a staff account must never cascade into the trail.

    There is deliberately NO free-text field here. Spec 6.9 line 2027 forbids
    exposing warehouse notes to the customer, and the only way to keep that
    promise structurally is for the trail itself to have nowhere to put one.
    """

    shipment = models.ForeignKey(
        Shipment,
        on_delete=models.CASCADE,
        related_name="events",
    )

    from_status = models.CharField(
        max_length=20,
        choices=Shipment.Status.choices,
        null=True,
        blank=True,
    )

    to_status = models.CharField(
        max_length=20,
        choices=Shipment.Status.choices,
    )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shipment_events",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Oldest first: this ordering is what the customer trail reads
        # forward through, so it is the model's own rather than an admin-only
        # override. The id breaks ties between events written in one
        # transaction with equal timestamps.
        ordering = ("created_at", "id")
        verbose_name = "shipment event"
        verbose_name_plural = "shipment events"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise TypeError("ShipmentEvent rows are append-only")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.shipment_id}: {self.from_status}->{self.to_status}"
