"""PRISTINE-02: every DRF view is fail-closed unless it is deliberately public.

``REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"]`` is now ``IsAuthenticated``, so a
view that declares no ``permission_classes`` no longer inherits DRF's
``AllowAny`` default. This module proves that property over the WHOLE URL tree
instead of over the views someone remembered to list: it walks ``config.urls``
through every ``include()``, resolves the effective permission classes exactly
as ``APIView.as_view()`` does, and asks each permission set -- through DRF's own
``check_permissions`` loop, with an anonymous request -- whether an anonymous
caller gets in.

A view that admits an anonymous caller must appear in ``ALLOWLIST`` with the
reason it is public. The allowlist is closed in both directions: every listed
path must still be anonymously reachable and must resolve to one of the three
reviewed public shapes (``AllowAny``; safe-methods-public; submitted-email), so
an exemption cannot outlive the behaviour it was granted for.

Non-vacuity, stated:
* ``PermissionProbeTests`` runs the same probe against hand-written allow and
  deny control views, so a probe that returned a constant fails there;
  ``test_undeclared_view_is_closed_by_the_flip`` builds an ``APIView`` subclass
  that declares nothing and requires the flipped default to close it (and would
  have failed before the flip, when the inherited default was ``AllowAny``);
* ``test_the_walk_crosses_includes_and_skips_plain_views`` pins that the walk
  entered the nested ``/api/v1/`` families and classified both DRF and plain
  Django callbacks, and fixes a floor on how many DRF endpoints it found;
* the allowlist checks run in both directions, so the probe cannot be satisfied
  by returning ``True`` everywhere (the outside-the-allowlist check would go
  red) or ``False`` everywhere (the allowlist-exactness check would go red).

Plain Django views (the admin chrome, ``/health/``, media) are out of scope and
are skipped explicitly by :func:`_drf_endpoint`.
"""

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.exceptions import APIException
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from common.permissions import HasProductsWriteOrReadOnly
from products.restock_views import IsAuthenticatedOrSubmittedEmail

# A body carrying an email: the one anonymous request shape a permission class
# in this tree inspects (the restock opt-in), so probing with it cannot miss a
# view that admits anonymous callers only when the body supplies that proof.
_PROBE_BODY = {"email": "probe@example.com"}
_PROBE_METHODS = ("GET", "POST", "PATCH", "DELETE")


def _aliases(legacy, v1, reason):
    """Both mounts of one urlconf object (``config/urls.py`` re-uses them)."""
    return {
        legacy: reason,
        v1: "v1 alias of %s: the same view object, included twice." % legacy,
    }


# Every path below admits an anonymous caller on purpose. Each entry names the
# reason in one line; the v1 entries are the same view objects included a
# second time by config/urls.py, not a second decision to be public.
ALLOWLIST = {}
ALLOWLIST.update(
    _aliases(
        "/api/accounts/register/",
        "/api/v1/account/register/",
        "Public: the account does not exist yet, so there is no credential.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/accounts/username-available/",
        "/api/v1/account/username-available/",
        "Public: availability is asked before the account exists.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/accounts/verify-email/",
        "/api/v1/account/verify-email/",
        "Public: the emailed code is the credential this endpoint presents.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/accounts/resend-verification/",
        "/api/v1/account/resend-verification/",
        "Public: recovery for a caller who cannot sign in yet.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/accounts/forgot-username/",
        "/api/v1/account/forgot-username/",
        "Public: recovery starts from a caller with no usable credential.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/accounts/password-reset/",
        "/api/v1/account/password-reset/",
        "Public: the reset flow starts from a caller who cannot sign in.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/accounts/password-reset/confirm/",
        "/api/v1/account/password-reset/confirm/",
        "Public: the single-use reset token is the credential being presented.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/accounts/login/",
        "/api/v1/account/login/",
        "Public: a login door cannot require the credential it issues.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/accounts/storefront/login/",
        "/api/v1/account/storefront/login/",
        "Public: the customer door, the same credential-free surface as login.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/accounts/token/refresh/",
        "/api/v1/account/token/refresh/",
        "Public: a caller whose access token expired has no live credential.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/accounts/mfa/setup/",
        "/api/v1/account/mfa/setup/",
        "Public: an unconfirmed device holds no JWT; enrollment proof is the "
        "credential (see _authorize_enrollment).",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/accounts/mfa/confirm/",
        "/api/v1/account/mfa/confirm/",
        "Public: the same enrollment proof -- a pending device cannot hold a JWT.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/cart/",
        "/api/v1/store/cart/",
        "Public: the guest cart is session-scoped, not account-scoped.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/cart/coupon/",
        "/api/v1/store/cart/coupon/",
        "Public: the coupon rides the session cart a guest owns.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/cart/coupon/",
        "/api/v1/store/cart/coupon/",
        "Public: the coupon rides the session cart a guest owns.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/cart/<int:item_id>/",
        "/api/v1/store/cart/<int:item_id>/",
        "Public: the guest cart is session-scoped, not account-scoped.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/orders/checkout/",
        "/api/v1/store/orders/checkout/",
        "Public: guests check out with no account; the guest order token is "
        "the credential.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/orders/guest/",
        "/api/v1/store/orders/guest/",
        "Public: the guest order token is the credential; an account order "
        "needs its bearer.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/orders/guest/<str:order_number>/",
        "/api/v1/store/orders/guest/<str:order_number>/",
        "Public: the guest order token is the credential; an account order "
        "needs its bearer.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/orders/apply-coupon/",
        "/api/v1/store/orders/apply-coupon/",
        "Public: the coupon is validated against the guest's session cart.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/shipping/estimate/",
        "/api/v1/store/shipping/estimate/",
        "Public: a price quote is needed before an account exists.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/shipping/track/<str:order_number>/",
        "/api/v1/store/shipping/track/<str:order_number>/",
        "Public: the 256-bit guest token is the credential; the view scopes by it.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/products/",
        "/api/v1/store/products/",
        "Public read: the catalogue is browsable anonymously; writes still need "
        "products.write.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/products/<slug:slug>/",
        "/api/v1/store/products/<slug:slug>/",
        "Public read: the catalogue is browsable anonymously; writes still need "
        "products.write.",
    )
)
ALLOWLIST.update(
    _aliases(
        "/api/products/<slug:slug>/restock-notifications/",
        "/api/v1/store/products/<slug:slug>/restock-notifications/",
        "Public: an anonymous opt-in is admitted only when the body carries a "
        "submitted email; GET and email-less POST stay auth-only.",
    )
)
ALLOWLIST["/api/v1/webhooks/razorpay/"] = (
    "Public: the provider's signature over the raw body is the only credential."
)

# The three reviewed public shapes. A path may only be allowlisted when its
# resolved permission set is one of these; anything else is an unreviewed gap,
# not an exemption.
_REVIEWED_SHAPES = (
    (AllowAny,),
    (HasProductsWriteOrReadOnly,),
    (IsAuthenticatedOrSubmittedEmail,),
)


def _join(prefix, pattern):
    return prefix + str(pattern)


def _walk(patterns, prefix=""):
    """Yield ``(path, entry)`` for every URL pattern, recursing into includes."""
    for entry in patterns:
        route = _join(prefix, entry.pattern)
        if isinstance(entry, URLResolver):
            yield from _walk(entry.url_patterns, route)
        elif isinstance(entry, URLPattern):
            yield "/" + route.lstrip("/"), entry


def _drf_endpoint(entry):
    """``(cls, initkwargs, actions)`` for a DRF callback, else ``None``.

    ``APIView.as_view`` stamps ``cls`` (and the initkwargs) onto the callback it
    returns, which is how a URL pattern is told apart from a plain Django view
    without importing every view module by hand. ``actions`` is set only by
    ``ViewSetMixin.as_view`` and is what makes a viewset's per-action
    ``get_permissions`` resolvable outside a real dispatch.
    """
    callback = entry.callback
    cls = getattr(callback, "cls", None)
    if not isinstance(cls, type) or not issubclass(cls, APIView):
        return None
    return (
        cls,
        dict(getattr(callback, "initkwargs", {}) or {}),
        getattr(callback, "actions", None),
    )


def _effective_permission_classes(cls, initkwargs):
    """Resolve the classes DRF would instantiate for this view.

    Mirrors ``as_view``: an initkwarg wins, otherwise the class attribute --
    which is DRF's ``DEFAULT_PERMISSION_CLASSES`` when the view declares none.
    """
    return tuple(initkwargs.get("permission_classes", cls.permission_classes))


def _permission_probe(cls, initkwargs, method, body=None, actions=None):
    """True when DRF's own permission loop admits an anonymous request."""
    factory = APIRequestFactory()
    if method == "GET":
        raw = factory.get("/probe/")
    else:
        raw = getattr(factory, method.lower())("/probe/", body, format="json")
    view = cls(**initkwargs)
    if actions is not None:
        view.action_map = dict(actions)
    view.request = view.initialize_request(raw)
    view.args = ()
    view.kwargs = {}
    try:
        view.check_permissions(view.request)
    except APIException:
        return False
    return True


def _admits_anonymous(cls, initkwargs, actions=None):
    return any(
        _permission_probe(
            cls,
            initkwargs,
            method,
            None if method == "GET" else _PROBE_BODY,
            actions,
        )
        for method in _PROBE_METHODS
    )


def _discovered_views():
    """``{path: (cls, initkwargs, actions)}`` for every DRF endpoint."""
    found = {}
    for path, entry in _walk(get_resolver().url_patterns):
        endpoint = _drf_endpoint(entry)
        if endpoint is not None:
            found[path] = endpoint
    return found


class PermissionProbeTests(SimpleTestCase):
    """The instrument itself, measured against hand-written controls."""

    def test_default_permission_class_is_is_authenticated(self):
        self.assertEqual(
            list(settings.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"]),
            ["rest_framework.permissions.IsAuthenticated"],
        )

    def test_undeclared_view_is_closed_by_the_flip(self):
        class DeclaresNothing(APIView):
            def get(self, request):
                return Response({})

        self.assertEqual(
            _effective_permission_classes(DeclaresNothing, {}),
            (IsAuthenticated,),
        )
        self.assertFalse(_permission_probe(DeclaresNothing, {}, "GET"))
        self.assertFalse(_permission_probe(DeclaresNothing, {}, "POST", _PROBE_BODY))

    def test_probe_admits_a_public_control_and_refuses_a_private_one(self):
        class AlwaysPublic(APIView):
            permission_classes = [AllowAny]

        class AlwaysPrivate(APIView):
            permission_classes = [IsAuthenticated]

        for method in _PROBE_METHODS:
            body = None if method == "GET" else _PROBE_BODY
            with self.subTest(method=method):
                self.assertTrue(_permission_probe(AlwaysPublic, {}, method, body))
                self.assertFalse(_permission_probe(AlwaysPrivate, {}, method, body))

    def test_an_empty_permission_list_is_not_a_bypass(self):
        class EmptiedOut(APIView):
            permission_classes = []

        # DRF runs no check at all for an empty list, so this shape must never
        # appear in the tree: it would survive any default and admit everyone.
        self.assertTrue(_permission_probe(EmptiedOut, {}, "GET"))


class UrlTreeTests(SimpleTestCase):
    """The walk reaches the whole tree and classifies both kinds of view."""

    def test_the_walk_crosses_includes_and_skips_plain_views(self):
        discovered = _discovered_views()
        plain = [
            path
            for path, entry in _walk(get_resolver().url_patterns)
            if _drf_endpoint(entry) is None
        ]
        self.assertIn("/health/", plain)
        self.assertGreaterEqual(len(discovered), 80)
        self.assertIn("/api/orders/", discovered)
        self.assertTrue(
            any(path.startswith("/api/v1/store/") for path in discovered),
            "the walk never entered the /api/v1/store/ include()",
        )
        self.assertTrue(
            any(path.startswith("/api/v1/account/") for path in discovered),
            "the walk never entered the /api/v1/account/ include()",
        )

    def test_no_view_declares_an_empty_permission_list(self):
        emptied = {
            path: cls.__name__
            for path, (cls, initkwargs, _actions) in _discovered_views().items()
            if _effective_permission_classes(cls, initkwargs) == ()
        }
        self.assertEqual(emptied, {}, "an empty permission list bypasses DRF checks")


class AllowlistTests(SimpleTestCase):
    """Both directions of the allowlist, over the real URL tree."""

    def test_every_drf_view_refuses_anonymous_unless_allowlisted(self):
        unlisted = {
            path: cls.__name__
            for path, (cls, initkwargs, actions) in _discovered_views().items()
            if path not in ALLOWLIST and _admits_anonymous(cls, initkwargs, actions)
        }
        self.assertEqual(
            unlisted,
            {},
            "these DRF endpoints admit anonymous callers without an allowlist "
            "entry and a stated reason",
        )

    def test_every_allowlisted_path_still_resolves_to_a_reviewed_public_shape(self):
        discovered = _discovered_views()
        for path, reason in ALLOWLIST.items():
            with self.subTest(path=path):
                self.assertTrue(reason and reason.strip(), "an entry needs a reason")
                self.assertIn(path, discovered, "allowlist entry matches no DRF view")
                cls, initkwargs, actions = discovered[path]
                resolved = _effective_permission_classes(cls, initkwargs)
                self.assertIn(
                    resolved,
                    _REVIEWED_SHAPES,
                    "allowlisted view resolves to an unreviewed permission set",
                )
                self.assertTrue(
                    _admits_anonymous(cls, initkwargs, actions),
                    "the exemption outlived the public behaviour it was granted for",
                )

    def test_the_reviewed_shapes_are_measured_not_asserted(self):
        discovered = _discovered_views()

        for path, (cls, initkwargs, actions) in discovered.items():
            if path not in ALLOWLIST:
                continue
            resolved = _effective_permission_classes(cls, initkwargs)
            if resolved == (AllowAny,):
                for method in _PROBE_METHODS:
                    body = None if method == "GET" else _PROBE_BODY
                    with self.subTest(path=path, method=method):
                        self.assertTrue(
                            _permission_probe(cls, initkwargs, method, body, actions)
                        )
            elif resolved == (HasProductsWriteOrReadOnly,):
                with self.subTest(path=path, method="GET"):
                    self.assertTrue(
                        _permission_probe(cls, initkwargs, "GET", None, actions)
                    )
                for method in ("POST", "PATCH", "DELETE"):
                    with self.subTest(path=path, method=method):
                        self.assertFalse(
                            _permission_probe(
                                cls, initkwargs, method, _PROBE_BODY, actions
                            )
                        )
            elif resolved == (IsAuthenticatedOrSubmittedEmail,):
                with self.subTest(path=path, method="GET"):
                    self.assertFalse(
                        _permission_probe(cls, initkwargs, "GET", None, actions)
                    )
                with self.subTest(path=path, method="POST with email"):
                    self.assertTrue(
                        _permission_probe(cls, initkwargs, "POST", _PROBE_BODY, actions)
                    )
                with self.subTest(path=path, method="POST without email"):
                    self.assertFalse(
                        _permission_probe(cls, initkwargs, "POST", {}, actions)
                    )

    def test_known_authenticated_and_privileged_views_are_not_allowlisted(self):
        unlisted = _discovered_views()
        for path in (
            "/api/orders/",
            "/api/accounts/addresses/",
            "/api/accounts/mfa/status/",
            "/api/admin/orders/",
        ):
            with self.subTest(path=path):
                self.assertIn(path, unlisted)
                self.assertNotIn(path, ALLOWLIST)
                cls, initkwargs, actions = unlisted[path]
                self.assertFalse(_admits_anonymous(cls, initkwargs, actions))
