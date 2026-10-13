"""Restock opt-in REST surface (SPEC-19-4, [R-19.11]).

POST /api/products/<slug>/restock-notifications/   opt in
DELETE /api/products/<slug>/restock-notifications/ opt out

Both require authentication and scope every operation to the caller
(``request.user``) — ownership is the filter, never a body-supplied user
field. Opt-in is refused for products that are in stock (there is nothing
to be notified about; the customer can simply buy it) and accepted
idempotently otherwise: the (user, product) unique constraint is the
concurrency authority, with get_or_create's IntegrityError retry semantics
doing the upsert. Opt-out is a no-op-safe 200 (uniform response, no
existence leak — deleting a nonexistent preference is still "opted out").
"""

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_scope
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import RestockNotification, products


def _uniform_not_found():
    """The one miss this surface answers.

    A DELISTED product must be indistinguishable from a nonexistent one
    here (MERCHANT-DELIST-C2), and two branches returning "the same"
    response is how that claim rots -- so the miss is built in exactly
    one place and both callers share it. The detail GET one endpoint
    over already answers the byte-identical 404 an unknown slug gets;
    an opt-in that answered 201 with the product's id would oracle
    "exists but off sale" against "never existed", defeating it.
    """
    return Response(
        {"error": "Product not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


@api_view(["POST", "DELETE"])
@permission_classes([IsAuthenticated])
@throttle_scope("restock")
def restock_notification(request, slug):
    """Customer opt-in/opt-out for one product's back-in-stock email."""
    try:
        product = products.objects.get(slug=slug)
    except products.DoesNotExist:
        return _uniform_not_found()

    if request.method == "POST":
        if not product.is_active:
            # A delisted product is not an offer: same miss as an unknown
            # slug (see _uniform_not_found), and nothing is armed — there
            # is nothing to be notified about for a product that is not
            # for sale. A customer whose opt-in predates the delist keeps
            # their armed row: the trigger suppresses its mail while the
            # product is off sale and the relist releases it (see
            # products.models._notify_back_in_stock).
            return _uniform_not_found()
        if product.stock > 0:
            return Response(
                {"error": "Product is in stock; no restock notification needed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # get_or_create re-checks existence on IntegrityError (the
        # unique constraint is the race authority). A re-opt-in flips
        # the row back to armed: active=True, notified_at cleared.
        with transaction.atomic():
            preference, created = RestockNotification.objects.get_or_create(
                user=request.user,
                product=product,
                defaults={"active": True, "notified_at": None},
            )
            if not created:
                preference.active = True
                preference.notified_at = None
                preference.save(update_fields=["active", "notified_at"])
        return Response(
            {
                "product_id": product.id,
                "slug": product.slug,
                "active": True,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    # DELETE — uniform response whether or not a preference exists: an
    # anonymous-shaped existence probe gains nothing, and the caller's
    # end state is identical ("not opted in").
    RestockNotification.objects.filter(user=request.user, product=product).update(
        active=False, notified_at=None
    )
    return Response({"active": False})
