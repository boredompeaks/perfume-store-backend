"""Link legacy ``Shipment.carrier`` text to a ``ShippingProvider`` row.

Before SPEC-B-02 a parcel carried only the carrier's display name, so the
tracking page had no provider to render a link from. The provider FK is the new
authority, and this module is the one mapper used by the backfill command (and
mirrored, for historical-model safety, in the migration).

The rule is deliberately conservative: a carrier value maps only when exactly
one provider matches it, either case-insensitively by ``code`` or exactly by
``name``. Ambiguity is left unresolved rather than guessed, because linking the
wrong carrier would send a paying customer to somebody else's tracking page.
"""

from .models import Shipment, ShippingProvider


def matching_provider_ids(carrier, providers):
    """Every provider id ``carrier`` names, with no guessing.

    ``code`` is compared case-insensitively because it is a machine identifier
    that operators type by hand; ``name`` is compared exactly because it is the
    human label the old ``carrier`` column held verbatim. Both comparisons run
    together, so a value that matches one provider by code and another by name
    is ambiguous by construction.
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

    Only parcels whose ``provider`` is still NULL are considered: a link an
    operator (or an earlier run) already made is a decision, and a backfill is
    not allowed to overwrite it. ``apply=False`` runs the identical matching
    pass and writes nothing, so the dry-run and the real run cannot disagree
    about what would happen.

    Idempotence is a property of the filter rather than of a marker column:
    after an apply the matched rows no longer have a NULL provider, so a second
    run sees nothing to do and reports zero matches.
    """
    providers = list(ShippingProvider.objects.all())
    counts = {"matched": 0, "ambiguous": 0, "unmatched": 0, "already_linked": 0}
    counts["already_linked"] = Shipment.objects.filter(
        provider__isnull=False
    ).count()

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
