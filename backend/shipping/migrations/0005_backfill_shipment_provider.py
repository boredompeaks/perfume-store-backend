"""Backfill ``Shipment.provider`` from the legacy ``carrier`` text.

SPEC-B-02 added the provider FK, but every parcel written before it carries
only the carrier's display name. This data migration links the rows it can be
certain about and leaves the rest NULL, exactly as the operational command does:

* the comparison is case-insensitive against ``ShippingProvider.code`` or
  exact against ``ShippingProvider.name``;
* a carrier that names more than one provider is AMBIGUOUS and stays NULL -
  guessing would send a customer to the wrong carrier's tracking page;
* a parcel that already has a provider is never rewritten.

The reverse operation is a no-op, documented rather than implied. This mapping
is not invertible: NULL is also the legitimate state of a parcel no operator has
linked yet, so clearing every provider on reverse would destroy links the
backfill never created. A data migration that cannot restore the previous state
must say so instead of pretending.
"""

from django.db import migrations


def _matching_provider_ids(carrier, providers):
    """The providers ``carrier`` names, using the historical rows only.

    Duplicated from ``shipping.backfill`` on purpose: a migration must not
    import live app code, because that code moves while this migration is
    frozen against the schema it was written for.
    """
    carrier = (carrier or "").strip()
    if not carrier:
        return set()
    folded = carrier.casefold()
    return {
        provider.pk
        for provider in providers
        if provider.code.casefold() == folded or provider.name == carrier
    }


def backfill_shipment_providers(apps, schema_editor):
    Shipment = apps.get_model("shipping", "Shipment")
    ShippingProvider = apps.get_model("shipping", "ShippingProvider")
    providers = list(ShippingProvider.objects.all())
    if not providers:
        return

    for shipment in Shipment.objects.filter(provider__isnull=True):
        matches = _matching_provider_ids(shipment.carrier, providers)
        if len(matches) == 1:
            # Queryset update on the historical model: this function is the
            # only writer, and it must not disturb ``updated_at`` because
            # linking a legacy row is not an operator edit of the parcel.
            Shipment.objects.filter(pk=shipment.pk).update(provider_id=matches.pop())


class Migration(migrations.Migration):
    dependencies = [
        ("shipping", "0004_shipment_provider"),
    ]

    operations = [
        migrations.RunPython(
            backfill_shipment_providers,
            # Not invertible - see the module docstring. NULL means both
            # "unmatched" and "never linked", so a reverse could not tell
            # which providers this migration added.
            migrations.RunPython.noop,
        ),
    ]
