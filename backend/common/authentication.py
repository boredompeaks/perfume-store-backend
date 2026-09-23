"""SPEC-17-03 [R-17.18]: CSRF enforcement for session-cookie mutations.

Django's CsrfViewMiddleware never sees DRF requests (``api_view`` marks
them csrf_exempt), so DRF re-enables the check inside its
``SessionAuthentication`` — but the stock class only protects requests
whose session carries a *logged-in user*, returning early for anonymous
ones. The cart's identity is the session itself: a guest cart is anonymous
yet session-cookie-authenticated for mutation purposes, and that exact
surface was live-confirmed exploitable (BACKEND_REQUESTS.md [P1]: POST and
DELETE on /api/cart/ with only the session cookie and no X-CSRFToken
returned 201/200). This class is the enforcement half of the fix; the
token pair (csrftoken cookie + X-CSRFToken header) is issued by the cart
GET surface and replayed by the SPA.
"""
from django.conf import settings
from rest_framework.authentication import SessionAuthentication


class SessionCartCSRFAuthentication(SessionAuthentication):
    """CSRF gate for every request that rides the session cookie.

    Runs in DEFAULT_AUTHENTICATION_CLASSES after JWTAuthentication, so
    JWT-authenticated requests never reach it: the chain stops at the first
    successful authenticator, and a bearer credential in the Authorization
    header cannot be attached by a cross-site request — there is nothing
    for CSRF to forge. Everything else that presents the session cookie
    (guest carts, session logins) gets Django's full CSRF check on unsafe
    methods via the inherited ``enforce_csrf``: the double-submit pair must
    match between the ``csrftoken`` cookie and the ``X-CSRFToken`` header,
    and safe methods pass through untouched (CSRFCheck itself only rejects
    unsafe ones).

    Returns ``None`` in every path: this is a pure gate, not an
    authentication source. Today's identity semantics are preserved
    exactly — JWT remains the only credential in the chain, and a
    session-authenticated request keeps resolving to the same DRF identity
    it had before this class existed — so no permission surface changes,
    only the CSRF enforcement is added.
    """

    def authenticate(self, request):
        user = getattr(request._request, "user", None)
        if user is not None and user.is_active:
            # Stock SessionAuthentication enforcement: a logged-in session
            # must present the token on unsafe methods.
            self.enforce_csrf(request)
            return None
        if settings.SESSION_COOKIE_NAME in request._request.COOKIES:
            # The guest-cart extension: an anonymous request carrying the
            # session cookie is exactly the cart-identity surface that was
            # live-confirmed exploitable, so it is held to the same
            # standard. A fresh visitor (no session cookie yet) is not
            # gated — no auth-bearing cookie rides the request, so there is
            # nothing to forge; the cart GET then issues both cookies and
            # every later mutation is protected.
            self.enforce_csrf(request)
        return None
