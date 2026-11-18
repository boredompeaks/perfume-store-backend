"""CSRF enforcement for session-cookie mutations.

DRF's stock ``SessionAuthentication`` skips anonymous requests, but the guest cart's
identity is the session itself; this gate covers it.
"""

from django.conf import settings
from rest_framework.authentication import SessionAuthentication


class SessionCartCSRFAuthentication(SessionAuthentication):
    """CSRF gate for every request that rides the session cookie.

    Runs after JWTAuthentication, so bearer requests short-circuit the chain and
    never reach it. Returns ``None`` always: this is a pure gate, not an
    authentication source.
    """

    def authenticate(self, request):
        user = getattr(request._request, "user", None)
        if user is not None and user.is_active:
            self.enforce_csrf(request)
            return None
        if settings.SESSION_COOKIE_NAME in request._request.COOKIES:
            # Guest-cart extension: an anonymous request carrying the session
            # cookie is held to the same standard. A fresh visitor has no
            # auth-bearing cookie, so there is nothing to forge.
            self.enforce_csrf(request)
        return None
