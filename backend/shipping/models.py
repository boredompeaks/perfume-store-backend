"""Shipping methods, their rates, and the parcels that leave on them.

A method is what a customer picks and a rate is what it costs for one
geography; weight-based rates are deliberately absent because the catalogue
has no weight column, only ``size``.
"""

from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


def _reject_oversized(model, field_name, value):
    """Raise when ``value`` is a string wider than the column it is written to.

    The bound is read off the field so refusal and schema cannot drift; only
    ``str`` is measured because ``bulk_update`` hands over expression objects.
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

    SQLite does not enforce ``varchar(n)`` while Postgres raises ``DataError``,
    so this makes the schema a boundary both engines agree on; queryset paths
    are covered in ``ShipmentQuerySet``.
    """
    for field in type(instance)._meta.concrete_fields:
        if not isinstance(field, models.CharField):
            continue
        _reject_oversized(type(instance), field.name, getattr(instance, field.attname))


class ShipmentQuerySet(models.QuerySet):
    """A ``Shipment`` queryset that will not move a status behind the trail.

    Status and its ``ShipmentEvent`` are written together in the admin; the
    bulk paths that never reach it are refused here, while other columns stay
    updatable.
    """

    def update(self, **kwargs):
        if "status" in kwargs:
            raise ValueError(
                "Shipment.status is moved only through the admin, which records "
                "the move as a ShipmentEvent; a bulk update cannot."
            )
        # The width gate rides here too: a bulk update writes straight to the
        # column, the same SQLite-vs-Postgres trap one layer down.
        for field_name, value in kwargs.items():
            _reject_oversized(Shipment, field_name, value)
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        """Refuse a status move here too, and width-gate the rest.

        ``update`` already catches it through Django's internals, but those are
        not a documented contract and Django wraps the call in a savepoint, so
        a refusal from inside would poison the caller's transaction; raising
        first is recoverable.
        """
        fields = list(fields)
        if "status" in fields:
            raise ValueError(
                "Shipment.status is moved only through the admin, which records "
                "the move as a ShipmentEvent; a bulk update cannot."
            )
        # The same width gate, because a bulk write does not call ``save``.
        for obj in objs:
            for field_name in fields:
                _reject_oversized(Shipment, field_name, getattr(obj, field_name))
        return super().bulk_update(objs, fields, batch_size=batch_size)


class ShipmentEventQuerySet(models.QuerySet):
    """The append-only guarantee on the paths a ``save()`` guard cannot see.

    Bulk ``update``/``delete``/``bulk_create``/``bulk_update`` never call the
    model methods, so all four are refused; Django's own delete collector and
    ``SET_NULL`` updates bypass these and still work.
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
        """Refuse every bulk update: every column of a step is part of the record."""
        raise ValueError(
            "ShipmentEvent rows are append-only: updating existing events in "
            "bulk is forbidden."
        )


class ShippingMethod(models.Model):
    """A delivery option a customer can pick, priced by its rates.

    ``code`` is the client-facing identifier checkout prices server-side, so
    it is part of the money path's trust boundary; it is staff-entered, and
    the unique index is the concurrency authority.
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
    # deactivation is the supported way to stop offering one.
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)

    def __str__(self):
        return self.name


class ShippingRate(models.Model):
    """What one method costs for one geography.

    A rate matches when both geography rules hold, where an empty rule is a
    wildcard; ``postal_code_prefix`` matches by prefix, so "400" covers the
    400xxx band.
    """

    method = models.ForeignKey(
        ShippingMethod,
        on_delete=models.CASCADE,
        related_name="rates",
    )

    # Exact decimal money, as every money column here: a float would be a
    # rounding error against the store.
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
            # One amount per method per geography: otherwise two matching rates
            # make a coin-toss winner the admin cannot see.
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
    """One parcel leaving the store for one order.

    One row per parcel, not per order; ``status`` is the shipment's own
    lifecycle and drives the order's visible state through
    ``shipping.lifecycle`` by reading ``orders.state`` rather than widening it.
    """

    class Status(models.TextChoices):
        LABEL_CREATED = "label_created", "Label created"
        DISPATCHED = "dispatched", "Dispatched"
        IN_TRANSIT = "in_transit", "In transit"
        DELIVERED = "delivered", "Delivered"
        EXCEPTION = "exception", "Exception"

    order = models.ForeignKey(
        # String ref avoids an import cycle: orders.models already points here.
        "orders.Order",
        # CASCADE by design: a parcel is a record OF one order, and order
        # hard-deletes are routine, so PROTECT would crash them.
        on_delete=models.CASCADE,
        related_name="shipments",
    )

    # UNIQUE: one reference must never answer for two parcels, and the backing
    # unique index serves the tracking lookup. Staff-entered, so no race.
    tracking_number = models.CharField(
        max_length=100,
        unique=True,
    )

    # Blank rather than required: an unbranded courier still hands out a reference.
    carrier = models.CharField(
        max_length=60,
        blank=True,
        default="",
    )

    # NULLABLE until linked; PROTECT because a careless delete must not
    # dead-end a shipped customer's tracking page — retire via ``is_active``.
    provider = models.ForeignKey(
        # App-local label declared later in this module; Django resolves it
        # lazily.
        "ShippingProvider",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="shipments",
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.LABEL_CREATED,
    )

    # NULL until a carrier commits to a date — a fact, not a sentinel date.
    estimated_delivery = models.DateTimeField(
        null=True,
        blank=True,
    )

    # Staff-only: spec forbids showing warehouse notes or carrier credentials
    # to customers, and one field keeps them off the tracking serializer.
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
        # No ``order`` traversal: this renders per row in admin/CSV/LogEntry,
        # and a guest order's own __str__ is not guaranteed null-safe.
        return f"Shipment {self.tracking_number} ({self.status})"

    @property
    def tracking_url(self):
        """The customer's tracking page for this parcel, or ``None``.

        Every unrenderable absence answers ``None``; ``str.replace`` rather
        than ``format`` because operator-entered URL text may contain ``{``.
        """
        if not self.provider_id or not self.tracking_number:
            return None
        template = self.provider.tracking_url_template
        if not template:
            return None
        rendered = template.replace(TRACKING_NUMBER_PLACEHOLDER, self.tracking_number)
        # Defense in depth: an ORM-written template skips ``clean()``, and a
        # javascript:/data: scheme in an ``href`` must become the same absence.
        parsed = urlsplit(rendered)
        if parsed.scheme not in _TRACKING_URL_SCHEMES or not parsed.netloc:
            return None
        return rendered

    def save(self, *args, **kwargs):
        """Refuse a value wider than the column, then write.

        The admin form validates itself, so a violation can only come from a
        direct ORM write — refused here so both engines enforce the boundary.
        """
        _reject_oversized_values(self)
        return super().save(*args, **kwargs)


class ShipmentEvent(models.Model):
    """One recorded status change on one shipment: the customer trail.

    Append-only on both the object path (save/delete) and the bulk path
    (``ShipmentEventQuerySet``), and deliberately has no free-text field so
    warehouse notes cannot leak into the trail.
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
        # Oldest first: the customer trail reads forward through it; id breaks
        # ties for events written in one transaction.
        ordering = ("created_at", "id")
        verbose_name = "shipment event"
        verbose_name_plural = "shipment events"

    objects = ShipmentEventQuerySet.as_manager()

    def save(self, *args, **kwargs):
        """Append only: a pk-set save would rewrite a recorded step."""
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


# The one placeholder a tracking template may carry: validator, renderer and
# help text must agree on its spelling.
TRACKING_NUMBER_PLACEHOLDER = "{tracking_number}"

# http is admitted only while DEBUG is on, so a production template cannot
# downgrade the customer's link to cleartext.
_TRACKING_URL_SCHEMES = ("http", "https")

# A URL-shaped sample, not a real reference: the template is validated on the
# provider, where no parcel exists.
_SAMPLE_TRACKING_NUMBER = "SAMPLE123"


def _tracking_template_error(template):
    """Why ``template`` cannot be a tracking URL, or ``None`` when it can.

    This is a security boundary (the result lands in an ``href``); exactly one
    placeholder is required because zero or two makes every parcel point at one
    page or the substitution ambiguous.
    """
    if not template:
        return None
    if any(character.isspace() or ord(character) < 32 for character in template):
        return "must not contain whitespace or control characters"
    occurrences = template.count(TRACKING_NUMBER_PLACEHOLDER)
    if occurrences != 1:
        return (
            "must contain exactly one {tracking_number} placeholder "
            f"(found {occurrences})"
        )
    rendered = template.replace(TRACKING_NUMBER_PLACEHOLDER, _SAMPLE_TRACKING_NUMBER)
    parsed = urlsplit(rendered)
    if parsed.scheme not in _TRACKING_URL_SCHEMES or not parsed.netloc:
        return "must be an absolute http(s) URL"
    if parsed.scheme != "https" and not settings.DEBUG:
        return "must use https:// (http:// is accepted only with DEBUG on)"
    return None


class ShippingProvider(models.Model):
    """A carrier / shipping provider admin can add, linkable to shipments.

    ``tracking_url_template`` renders the carrier's tracking page for a
    parcel; ``clean`` is its only validation and lives on the model so every
    ``full_clean`` writer gets the same answer.
    """

    name = models.CharField(
        max_length=100,
    )

    code = models.SlugField(
        max_length=40,
        unique=True,
    )

    tracking_url_template = models.CharField(
        max_length=500,
        blank=True,
        default="",
    )

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code",)

    def __str__(self):
        return self.name

    def clean(self):
        """Refuse a template that is not a safe, renderable http(s) URL.

        Runs on the admin form path, so an operator learns at entry time; the
        renderer keeps its own scheme guard for ORM-written templates.
        """
        super().clean()
        error = _tracking_template_error(self.tracking_url_template)
        if error:
            raise ValidationError({"tracking_url_template": error})
