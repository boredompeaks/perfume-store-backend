"""Link legacy ``Shipment.carrier`` text to a ``ShippingProvider`` row.

A carrier maps only when exactly one provider matches (code case-insensitively
or name exactly); ambiguity is left unresolved because linking the wrong
carrier sends a customer to someone else's tracking page.
"""

from .models import Shipment, ShippingProvider


def matching_provider_ids(carrier, providers):
    """Every provider id ``carrier`` names, with no guessing.

    ``code`` is compared case-insensitively (a hand-typed identifier); ``name``
    exactly (the verbatim old label), so a cross-match is ambiguous by
    construction.
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


def backfill_shipments(*, apply):
    """Map every unlinked parcel, returning ``matched/ambiguous/unmatched``.

    Only NULL-provider parcels are considered — an existing link is a decision
    — and ``apply=False`` runs the identical pass without writing, so dry run
    and apply cannot disagree.
    """
    providers = list(ShippingProvider.objects.all())
    counts = {"matched": 0, "ambiguous": 0, "unmatched": 0, "already_linked": 0}
    counts["already_linked"] = Shipment.objects.filter(provider__isnull=False).count()

    for shipment in Shipment.objects.filter(provider__isnull=True):
        matches = matching_provider_ids(shipment.carrier, providers)
        if len(matches) == 1:
            counts["matched"] += 1
            if apply:
                shipment.provider_id = matches.pop()
                shipment.save(update_fields=["provider"])
        elif len(matches) > 1:
            counts["ambiguous"] += 1
        else:
            counts["unmatched"] += 1
    return counts
