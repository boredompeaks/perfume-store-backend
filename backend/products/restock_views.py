"""Restock opt-in/opt-out REST surface for one product.

Guest POSTs may arm an email-keyed row, but every guest response is uniform by
design so the surface is not a format/history/registration oracle; opt-out
stays ownership-scoped to the caller.
"""

import hashlib

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    permission_classes,
    throttle_classes,
    throttle_scope,
)
from rest_framework.exceptions import ParseError
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle

from .models import RestockNotification, products

# validate_email checks format, not length, and SQLite does not enforce
# varchar lengths — so the cap must live here or it binds on only one engine.
GUEST_EMAIL_MAX_LENGTH = 254


def _normalize_guest_email(raw):
    """Stored/validated form of a submitted address: strip + case-fold so case
    and padding variants converge on one row, throttle identity and unique key.
    """
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def _guest_email_is_valid(email):
    """Format gate before arming a guest row; the caller still answers the
    uniform success either way, so the response does not betray the format.
    """
    if not email or len(email) > GUEST_EMAIL_MAX_LENGTH:
        return False
    try:
        validate_email(email)
    except ValidationError:
        return False
    return True


def _uniform_not_found():
    """The one miss this surface answers.

    A delisted product must be indistinguishable from a nonexistent one, so
    the miss is built in exactly one place rather than duplicated per branch.
    """
    return Response(
        {"error": "Product not found"},
        status=status.HTTP_404_NOT_FOUND,
    )


class IsAuthenticatedOrSubmittedEmail(BasePermission):
    """POST is open to guests only when the body carries a submitted email.

    Failing returns DRF's standard NotAuthenticated (the same uniform 401 the
    former auth-only endpoint produced); ParseError is swallowed so body
    parseability is not a probe.
    """

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return True
        if request.method != "POST":
            return False
        try:
            data = request.data
        except ParseError:
            # No email in an unparseable body: deny via the normal
            # NotAuthenticated path (uniform 401); only ParseError is swallowed.
            return False
        raw = data.get("email") if hasattr(data, "get") else None
        # Same normalizer the view and throttle key on, so a non-string or
        # blank value is "no email" at every layer.
        return bool(_normalize_guest_email(raw))


class GuestPairThrottle(ScopedRateThrottle):
    """The per-(IP + email) budget for anonymous opt-ins.

    The pair key bounds repeat submissions without letting an attacker burn a
    victim address's budget; an email-only key would enable exactly that DoS.
    Authenticated callers return no key and use the user-keyed 'restock'
    budget.
    """

    pair_scope = "restock-guest"

    def allow_request(self, request, view):
        self.scope = self.pair_scope
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return SimpleRateThrottle.allow_request(self, request, view)

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return None
        data = request.data
        raw = data.get("email") if hasattr(data, "get") else None
        email = _normalize_guest_email(raw)
        ident = self.get_ident(request)
        # Hashed so the cache key never carries the raw address.
        digest = hashlib.sha256(f"{ident}:{email}".encode("utf-8")).hexdigest()[:32]
        return self.cache_format % {"scope": self.scope, "ident": digest}


@api_view(["POST", "DELETE"])
@permission_classes([IsAuthenticatedOrSubmittedEmail])
@throttle_classes([ScopedRateThrottle, GuestPairThrottle])
@throttle_scope("restock")
def restock_notification(request, slug):
    """Customer opt-in/opt-out for one product's back-in-stock email."""
    try:
        product = products.objects.get(slug=slug)
    except products.DoesNotExist:
        return _uniform_not_found()

    if request.method == "POST":
        if not product.is_active:
            # A delisted product is not an offer: same miss as an unknown slug,
            # nothing is armed; pre-existing armed rows survive the relist.
            return _uniform_not_found()
        if product.stock > 0:
            return Response(
                {"error": "Product is in stock; no restock notification needed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if request.user.is_authenticated:
            # get_or_create re-checks on IntegrityError (the constraint is the
            # race authority); a re-opt-in re-arms the row, ignoring any email.
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

        # Guest path: arm a row keyed by the normalized address. A malformed
        # address arms nothing yet still succeeds (no format/history oracle).
        email = _normalize_guest_email(request.data.get("email"))
        if _guest_email_is_valid(email):
            with transaction.atomic():
                preference, _ = RestockNotification.objects.get_or_create(
                    user=None,
                    email=email,
                    product=product,
                    defaults={"active": True, "notified_at": None},
                )
                if not preference.active or preference.notified_at is not None:
                    preference.active = True
                    preference.notified_at = None
                    preference.save(update_fields=["active", "notified_at"])
        return Response(
            {
                "product_id": product.id,
                "slug": product.slug,
                "active": True,
            },
            status=status.HTTP_202_ACCEPTED,
        )

    # Uniform response whether or not a preference exists; authentication is
    # already required and the filter keeps ownership scoped to the caller.
    RestockNotification.objects.filter(user=request.user, product=product).update(
        active=False, notified_at=None
    )
    return Response({"active": False})
