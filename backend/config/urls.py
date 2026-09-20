from django.contrib import admin
from django.urls import include, path

from django.conf import settings
from django.conf.urls.static import static

from ops.views import api_settings, audit_log, dashboard, health


# --- /api/v1/ namespace (spec §9, R-9.0) ---------------------------------
# Spec §9 prescribes versioned routes under /api/v1/ with four
# organizational prefixes: store (customer-facing storefront incl. cart and
# checkout, §9.1/§9.3), account (auth + account, §9.2), admin (§9.4) and
# webhooks (§9 line 2587). Prefixes are organizational boundaries, not
# authorization substitutes.
#
# The v1 mounts RE-USE the same urlconf objects as the legacy mounts below
# — no view duplication — so a route change in an app urlconf applies to
# both families at once. The legacy /api/... paths stay alive as aliases
# until the frontend base-URL cutover, which is coordinated separately
# (S9 ledger note). Namespaces keep reverse() unambiguous: legacy route
# names keep producing legacy paths (e.g. links inside password-reset
# emails) until that cutover.
#
# The webhooks family stays unrouted for now: no webhook endpoint exists
# yet and SPEC-1-06 owns that endpoint and its mount (§11/§17 cross-refs
# already require it to live under this namespace).

v1_store_patterns = [
    path("products/", include("products.urls")),
    path("cart/", include("cart.urls")),
    path("orders/", include("orders.urls")),
    # §9.1 route /store/config: the public settings reader the legacy
    # config mounts at /api/settings/.
    path("config/", api_settings, name="config"),
]

v1_account_patterns = [
    path("", include("accounts.urls")),
]

v1_admin_patterns = [
    path("dashboard/", dashboard, name="dashboard"),
    path("audit-log/", audit_log, name="audit-log"),
]

v1_urlpatterns = [
    path("store/", include((v1_store_patterns, "store"), namespace="store")),
    path("account/", include((v1_account_patterns, "account"), namespace="account")),
    path("admin/", include((v1_admin_patterns, "admin"), namespace="admin")),
]


urlpatterns = [
    # Store dashboard (staff-only) — must be registered BEFORE the admin
    # include, or the admin's URLconf swallows it and returns 404.
    path("admin/dashboard/", dashboard, name="admin-dashboard"),

    # Audit log (spec 6.12 route /admin/audit-log) — same before-the-admin
    # include rule as the dashboard above.
    path("admin/audit-log/", audit_log, name="admin-audit-log"),

    path("admin/", admin.site.urls),

    path("health/", health, name="health"),
    path("api/settings/", api_settings, name="api-settings"),

    path("api/products/", include("products.urls")),

    path("api/cart/", include("cart.urls")),

    path("api/orders/", include("orders.urls")),

    path("api/accounts/", include("accounts.urls")),

    # /api/v1/ namespace (spec §9, R-9.0): same urlconf objects as the
    # legacy mounts above; legacy paths remain alive as aliases.
    path("api/v1/", include((v1_urlpatterns, "v1"), namespace="v1")),
]


urlpatterns += static(
    settings.MEDIA_URL,
    document_root=settings.MEDIA_ROOT
)
