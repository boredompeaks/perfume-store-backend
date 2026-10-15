"""Restock opt-in REST surface (SPEC-19-4, [R-19.11]).

POST /api/products/<slug>/restock-notifications/   opt in
DELETE /api/products/<slug>/restock-notifications/ opt out

SPEC-Q-RS1 (BE-012): the POST half opens to guests — an unauthenticated
caller who submits an email arms a GUEST row (user NULL, normalized
address in the model's email column) and the restock trigger mails THAT
address; the authenticated path is byte-compatible with the pre-task
contract and keeps mailing user.email, ignoring any submitted address
entirely. Uniformity is the security property of the guest half: every
guest success is one 202 with one body — valid or invalid format, first
or repeat, registered address or not — so the surface cannot be steered
into a format, history or registration oracle. A guest POST without an
email, and every guest DELETE, keep the exact 401 the endpoint answered
when it was auth-only (opt-out stays ownership-scoped to request.user;
a guest row has no caller identity to scope a DELETE by, and accepting
an email on DELETE would let anyone disarm anyone's notification).

Authorization stays in permission_classes (never an inline user check):
authenticated callers pass for both methods; anonymous callers pass only
for a POST whose body carries a non-empty string email. Abuse stays
budgeted on two axes under env-driven rates: the pre-existing 'restock'
scope (per-IP for anonymous, per-user for authed — volume bound) plus
the 'restock-guest' scope, a per-(IP + email) pair budget that bounds
repeat submissions of one pair without letting an attacker starve a
victim address globally (an email-ONLY key would enable that DoS).
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
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, SimpleRateThrottle

from .models import RestockNotification, products

# The model column's own bound: validate_email checks the format, not the
# length, and SQLite does not enforce varchar lengths — so the length cap
# has to live here or an overlong "address" would be stored (and armed)
# on one engine and not the other.
GUEST_EMAIL_MAX_LENGTH = 254


def _normalize_guest_email(raw):
    """The stored and validated form of a submitted address: strip + case
    fold, so case and padding variants converge on one row, one throttle
    identity, and one unique-constraint key."""
    if not isinstance(raw, str):
        return ""
    return raw.strip().lower()


def _guest_email_is_valid(email):
    """Format gate for arming a guest row — never trust a submitted
    address blindly. False for empties, overlongs and malformed shapes;
    the caller still answers the uniform success either way (the response
    must not betray the format)."""
    if not email or len(email) > GUEST_EMAIL_MAX_LENGTH:
        return False
    try:
        validate_email(email)
    except ValidationError:
        return False
    return True


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


class IsAuthenticatedOrSubmittedEmail(BasePermission):
    """POST is open to guests only when the body carries a submitted
    email; DELETE and email-less POST keep the auth-only contract.

    Failing this permission for an anonymous caller raises DRF's
    NotAuthenticated through the standard permission_denied path — the
    same exception (and therefore the same uniform envelope bytes) the
    endpoint's former IsAuthenticated class produced, which is what the
    prior-shape pins measure.
    """

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return True
        if request.method != "POST":
            return False
        data = request.data
        raw = data.get("email") if hasattr(data, "get") else None
        # Same normalizer the view and the throttle key on, so a non-string
        # or blank value is "no email" at every layer — one definition, no
        # layer disagreeing about what counts as submitted.
        return bool(_normalize_guest_email(raw))


class GuestPairThrottle(ScopedRateThrottle):
    """The per-(IP + email) budget for anonymous opt-ins (SPEC-Q-RS1).

    Keyed on the client identity AND the normalized submitted address, so
    repeat submissions of one pair are bounded while distinct addresses
    from one origin are left to the per-IP 'restock' budget (both run —
    DRF applies every throttle class and any refusal wins). An
    email-ONLY key would let an attacker burn a victim address's whole
    budget and starve the victim's own opt-in; the pair key cannot.

    Authenticated callers return no key here: their email is ignored by
    the view and their user-keyed 'restock' budget is the unchanged,
    pre-task bound.

    The scope is pinned to this class rather than read off the view (both
    throttle classes share one @throttle_scope) so the pair budget keeps
    its own env-driven knob: a deployment can tighten the pair bound (the
    mail-bomb shape — one origin re-arming one address) independently of
    the per-IP volume budget the authed path also draws on.
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
        if request.user.is_authenticated:
            # get_or_create re-checks existence on IntegrityError (the
            # unique constraint is the race authority). A re-opt-in flips
            # the row back to armed: active=True, notified_at cleared.
            # The submitted email, if any, is ignored: an account row's
            # address is user.email and always has been.
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

        # Guest path: arm a row keyed by the normalized address. A
        # malformed or overlong address arms NOTHING (never trust it
        # blindly) but still gets the byte-identical success — the
        # response must not double as a format checker. Repeat and
        # first-time are indistinguishable for the same reason (no
        # history oracle), hence the single 202 rather than the authed
        # path's 201/200 split.
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

    # DELETE — uniform response whether or not a preference exists: an
    # anonymous-shaped existence probe gains nothing, and the caller's
    # end state is identical ("not opted in"). Authentication was already
    # required by the permission class; the filter keeps ownership
    # scoped to the caller (guest rows have no caller and are unreachable
    # here by design).
    RestockNotification.objects.filter(user=request.user, product=product).update(
        active=False, notified_at=None
    )
    return Response({"active": False})
