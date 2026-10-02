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


def _reject_oversized(model, field_name, value):
    """Raise when ``value`` is a string wider than the column it is written to.

    The bound is read off the field itself rather than restated as a literal,
    so the refusal and the schema cannot drift apart, and a field with no
    width is never checked because there is nothing to exceed.

    Only ``str`` is measured. Django routes the values of a ``bulk_update``
    through the same ``QuerySet.update`` as a ``Case`` expression rather than
    as the string that produced it, so a value here is not always a value at
    all - and an expression's width is the database's business, not this
    gate's. ``None`` and every non-string fall out of the same check.
    """
    max_length = getattr(model._meta.get_field(field_name), "max_length", None)
    if max_length is None or not isinstance(value, str):
        return
    if len(value) > max_length:
        raise ValueError(
            f"{model.__name__}.{field_name} must be at most {max_length} characters"
        )


def _reject_oversized_values(instance):
    """Apply :func:`_reject_oversized` to every bounded column of ``instance``.

    SQLite does not enforce a ``varchar(n)`` width while Postgres does, so an
    over-length value saves cleanly in this test suite and raises ``DataError``
    on the production database - a 500 on a write the suite reported as a
    success.

    Called from both models' ``save``, which is the write boundary the admin
    form funnels into: the form validates its own fields, so a width that
    reaches the database can only have come from a direct ORM write. The
    queryset paths that bypass ``save`` are covered by the same helper in
    ``ShipmentQuerySet``.
    """
    for field in type(instance)._meta.concrete_fields:
        if not isinstance(field, models.CharField):
            continue
        _reject_oversized(type(instance), field.name, getattr(instance, field.attname))


class ShipmentQuerySet(models.QuerySet):
    """A ``Shipment`` queryset that will not move a status behind the trail.

    Spec 10.3 lists an "Audit record" as the last element of every transition's
    contract, and ``ShipmentAdmin.save_model`` is the writer that honours it:
    the status write and the ``ShipmentEvent`` that records it are one
    transaction. The bulk paths - ``update()`` and ``bulk_update()`` - are the
    writes that never reach that method, so both are refused here rather than
    left as a way to move a parcel with no record of where it moved from. The
    other columns stay updatable, because none of them is a transition the
    trail claims to hold.
    """

    def update(self, **kwargs):
        if "status" in kwargs:
            raise ValueError(
                "Shipment.status is moved only through the admin, which records "
                "the move as a ShipmentEvent; a bulk update cannot."
            )
        # The width gate rides here too, not only on save(): a bulk update
        # writes straight to the column, so without this a 400-character
        # carrier would be a clean write on SQLite and a DataError on the
        # production database - the same trap, one layer down.
        for field_name, value in kwargs.items():
            _reject_oversized(Shipment, field_name, value)
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        """Refuse a status move here too, and width-gate the rest.

        ``bulk_update`` is already caught by the ``update`` guard above, because
        Django compiles the values into a CASE expression and issues it through
        ``queryset.filter(...).update(...)`` (Django 6.1). It is refused HERE
        as well rather than left to that, for two reasons that are both about
        the caller rather than about the guard: the internal route is a Django
        implementation detail rather than a documented contract, and Django
        wraps the whole operation in ``transaction.atomic(savepoint=False)``, so
        a refusal raised from inside it poisons the caller's transaction - a
        caller could not catch the error and carry on. Raising before the block
        opens makes the refusal immediate and recoverable.
        """
        fields = list(fields)
        if "status" in fields:
            raise ValueError(
                "Shipment.status is moved only through the admin, which records "
                "the move as a ShipmentEvent; a bulk update cannot."
            )
        # The same width gate as ``update``/``save``, because a bulk write does
        # not call ``save`` either: the values ride in on the objects.
        for obj in objs:
            for field_name in fields:
                _reject_oversized(Shipment, field_name, getattr(obj, field_name))
        return super().bulk_update(objs, fields, batch_size=batch_size)


class ShipmentEventQuerySet(models.QuerySet):
    """The append-only guarantee on the paths a ``save()`` guard cannot see.

    ``ShipmentEvent.save`` refuses a re-save and ``ShipmentEvent.delete``
    refuses a delete, so the direct-object path is closed. A bulk
    ``update()``/``delete()``/``bulk_create()``/``bulk_update()`` writes rows
    without ever calling either, which is how a recorded step could be
    rewritten, erased or forged from a shell - so all four are refused here
    instead.

    Django's own machinery is unaffected: the delete collector and the
    ``SET_NULL`` field update both issue their own SQL and never route through
    ``QuerySet.delete()``/``QuerySet.update()``, so a deleted shipment still
    takes its trail with it (see the cascade policy on ``Shipment.order``).
    """

    def update(self, **kwargs):
        raise ValueError(
            "ShipmentEvent rows are append-only: updating an existing event is "
            "forbidden."
        )

    def delete(self):
        raise ValueError(
            "ShipmentEvent rows are append-only: deleting an event is forbidden."
        )

    def bulk_create(self, objs, **kwargs):
        raise ValueError(
            "ShipmentEvent rows are append-only: bulk-creating events is "
            "forbidden, because it bypasses the record-the-move path that is "
            "the only sanctioned writer."
        )

    def bulk_update(self, objs, fields, batch_size=None):
        """Refuse every bulk update, on ``ShipmentQuerySet``'s reasoning.

        Every column of a recorded step is part of the record - the status, the
        previous status, the parcel and the actor - so there is no field list
        that would be safe here, unlike ``Shipment.status`` on a parcel.
        """
        raise ValueError(
            "ShipmentEvent rows are append-only: updating existing events in "
            "bulk is forbidden."
        )


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
        # CASCADE, deliberately, and this is the recorded answer to spec 8.3
        # line 2485's "explicit deletion/archival policies": a shipment is a
        # record OF one order's fulfilment, so it has no referent once that
        # order is gone, and three of this codebase's own surfaces would break
        # if it were protected - `Shipment.__str__` and the changelist's
        # `order` column, `search_fields` on `order__order_number`, and the
        # tracking endpoint's own owner filter, all of which join to the order.
        #
        # It is NOT the same decision as `ShipmentEvent.actor`'s SET_NULL, and
        # the asymmetry is the point rather than an oversight. A staff account
        # is an ATTRIBUTION on a row that must outlive it; the order is the
        # row's subject, and this store hard-deletes orders as a matter of
        # course (`Order.user` is itself CASCADE, and the backup/restore drill
        # truncates the order table wholesale). PROTECT here would turn every
        # such delete into a crash on a table the system is designed to empty.
        #
        # Stated cost, not hidden: deleting an order takes its parcels and
        # their trail with it, so - unlike `common.models.AuditEvent`, whose
        # `order` is SET_NULL for exactly this reason - an order's fulfilment
        # history does not outlive the order. That is an accepted limit of a
        # store with no order archival yet (spec 8.3's archival half is
        # unbuilt), and the honest place to revisit it is `Order`, not here.
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

    objects = ShipmentQuerySet.as_manager()

    def __str__(self):
        # No `order` traversal: this label is what the admin grid, the CSV
        # export and LogEntry render per row, and an order can be a guest row
        # whose own __str__ already has to be null-safe (SPEC-1-B04).
        return f"Shipment {self.tracking_number} ({self.status})"

    def save(self, *args, **kwargs):
        """Refuse a value wider than the column, then write.

        The admin form validates its own fields, so a width violation can only
        reach this line from a direct ORM write - and SQLite would accept it
        where the production Postgres raises ``DataError`` (see
        ``_reject_oversized_values``). Refusing here makes the store's own
        schema the boundary both engines agree on, instead of a rule that
        happens to hold on the development database.
        """
        _reject_oversized_values(self)
        return super().save(*args, **kwargs)


class ShipmentEvent(models.Model):
    """[R-1.08] SPEC-1-B06: one recorded status change on one shipment.

    Spec 6.9 line 2015 names "Tracking events" as its own feature, and spec
    10.3 lists an "Audit record" as the last element of every transition's
    contract. This is that record, and every status change ``ShipmentAdmin``
    makes is remembered here and nowhere else: the customer trail the tracking
    endpoint serves is this table, so a shipment's history cannot be
    reconstructed differently from the history an operator saw.

    Append-only, on BOTH write paths, and the second one is the one a
    ``save()``-only guard misses. ``save()`` below rejects any pk-set re-save
    and ``delete()`` rejects any delete, which closes the direct-object path -
    the same pair ``common.models.AuditEvent`` uses. ``ShipmentEventQuerySet``
    above then closes the bulk path, which is how a recorded step could
    otherwise be rewritten, erased or forged without either method running:
    ``Model.objects.filter(...).update(...)`` writes rows through
    ``QuerySet.update`` and never calls ``save()``, ``bulk_update`` compiles a
    CASE expression and issues it as an UPDATE without calling ``save()``
    either, and ``bulk_create`` inserts rows with no model method at all. So no
    code path can rewrite, delete or forge a recorded step, through the object
    or through the queryset.

    What is NOT claimed: the admin registration is view-only, so history cannot
    be written or removed through the admin either, and deleting the SHIPMENT
    takes its trail with it (the CASCADE policy on ``Shipment.order`` says why).
    ``from_status`` is NULL exactly on the creation event (a shipment that has
    never left a previous state).

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

    objects = ShipmentEventQuerySet.as_manager()

    def save(self, *args, **kwargs):
        """Append only: a pk-set save would rewrite a recorded step.

        The direct-object half of the append-only guarantee, mirroring
        ``AuditEvent.save``. ``ShipmentEventQuerySet`` is the other half.
        """
        _reject_oversized_values(self)
        if self.pk is not None:
            raise ValueError(
                "ShipmentEvent rows are append-only: updating an existing "
                "event is forbidden."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Refuse to delete a recorded step, mirroring ``AuditEvent.delete``."""
        raise ValueError(
            "ShipmentEvent rows are append-only: deleting an event is forbidden."
        )

    def __str__(self):
        return f"{self.shipment_id}: {self.from_status}->{self.to_status}"
