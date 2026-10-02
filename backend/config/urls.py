import re

from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve as serve_media

from django.conf import settings

from ops.views import api_settings, audit_log, dashboard, health

from common.admin_search import global_search
from common.saved_filters import delete_saved_filter, save_saved_filter

from orders.views import (
    admin_order_cancel,
    admin_order_detail,
    admin_order_fulfill,
    admin_order_list,
    admin_order_refund,
)

# [R-1.15] SPEC-1-06: the payment webhook endpoint, mounted under the webhooks
# family §9 (line 2587) reserves. It is not one of the direct admin mounts
# above because it authenticates no user at all: its only credential is the
# provider's signature over the raw request body.
from orders.webhooks import razorpay_webhook

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
# The webhooks family holds one endpoint (SPEC-1-06): the payment gateway
# calling the server about a payment. It is a server-to-server call, so it
# gets no legacy alias and no dual mount — there is no frontend base URL to
# cut over here, only a provider URL to configure.

v1_store_patterns = [
    path("products/", include("products.urls")),
    path("cart/", include("cart.urls")),
    path("orders/", include("orders.urls")),
    # [R-1.07] SPEC-1-B05: spec 9.1 line 2703 `/store/shipping/estimate`,
    # mounted under the same store family as the cart and checkout it prices.
    path("shipping/", include("shipping.urls")),
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
    # §9.4 Orders module JSON seam (SPEC-9-07): DRF views wired exactly like
    # the two chrome routes above — direct mounts in both families, no view
    # duplication. The ledger's requirement names /api/admin/orders/, so the
    # admin family (not the store family) owns these routes.
    path("orders/", admin_order_list, name="orders-list"),
    path("orders/<int:order_id>/", admin_order_detail, name="orders-detail"),
    path("orders/<int:order_id>/fulfill/", admin_order_fulfill, name="orders-fulfill"),
    path("orders/<int:order_id>/cancel/", admin_order_cancel, name="orders-cancel"),
    # [R-1.14] SPEC-1-05: the refund seam, mounted beside the fulfil/cancel
    # edges it is the money-movement counterpart of (same admin family, same
    # direct-mount rule).
    path("orders/<int:order_id>/refund/", admin_order_refund, name="orders-refund"),
]

# [R-1.15] SPEC-1-06: the gateway's own callback, mounted under the family §9
# reserves for it. Named by provider so the family can hold the next one (a
# payments provider that is not Razorpay) without re-deciding the mount.
v1_webhook_patterns = [
    path("razorpay/", razorpay_webhook, name="razorpay-webhook"),
]

v1_urlpatterns = [
    path("store/", include((v1_store_patterns, "store"), namespace="store")),
    path("account/", include((v1_account_patterns, "account"), namespace="account")),
    path("admin/", include((v1_admin_patterns, "admin"), namespace="admin")),
    path(
        "webhooks/",
        include((v1_webhook_patterns, "webhooks"), namespace="webhooks"),
    ),
]


urlpatterns = [
    # Store dashboard (staff-only) — must be registered BEFORE the admin
    # include, or the admin's URLconf swallows it and returns 404.
    path("admin/dashboard/", dashboard, name="admin-dashboard"),

    # Audit log (spec 6.12 route /admin/audit-log) — same before-the-admin
    # include rule as the dashboard above.
    path("admin/audit-log/", audit_log, name="admin-audit-log"),

    # SPEC-5-10 (spec 5.1, "Search orders, products, customers…"): the ONE
    # global admin search. Gated by the disjunction of the three models' own
    # view capabilities, with each model's results gated individually by
    # its ModelAdmin — same before-the-admin include rule as the chrome
    # routes above.
    path("admin/search/", global_search, name="admin-global-search"),

    # SPEC-20-6 [R-20.11]: the saved-filter write endpoints — save the
    # current filter selection under a name, and drop one of the caller's
    # own. Applying a saved filter needs no route: it is the
    # `?_saved_filter=<pk>` marker the changelist itself reads, so the
    # saved view is reachable from the listing it filters. Both endpoints
    # gate on the target ModelAdmin's own view capability, and
    # must be registered BEFORE the admin include or it is swallowed.
    path(
        "admin/saved-filters/<slug:app_label>/<slug:model_name>/save/",
        save_saved_filter,
        name="admin-saved-filter-save",
    ),
    path(
        "admin/saved-filters/delete/<int:pk>/",
        delete_saved_filter,
        name="admin-saved-filter-delete",
    ),

    # §9.4 Orders module JSON seam (SPEC-9-07), legacy family: the alias of
    # the v1:admin orders mounts above (same view objects, no duplication).
    path("api/admin/orders/", admin_order_list, name="admin-orders-list"),
    path(
        "api/admin/orders/<int:order_id>/",
        admin_order_detail,
        name="admin-orders-detail",
    ),
    path(
        "api/admin/orders/<int:order_id>/fulfill/",
        admin_order_fulfill,
        name="admin-orders-fulfill",
    ),
    path(
        "api/admin/orders/<int:order_id>/cancel/",
        admin_order_cancel,
        name="admin-orders-cancel",
    ),
    # [R-1.14] SPEC-1-05: the legacy alias of the v1:admin refund mount above
    # (same view object, no duplication).
    path(
        "api/admin/orders/<int:order_id>/refund/",
        admin_order_refund,
        name="admin-orders-refund",
    ),

    path("admin/", admin.site.urls),

    path("health/", health, name="health"),
    path("api/settings/", api_settings, name="api-settings"),

    path("api/products/", include("products.urls")),

    path("api/cart/", include("cart.urls")),

    path("api/orders/", include("orders.urls")),

    # [R-1.07] SPEC-1-B05: the legacy alias of the v1:store shipping mount
    # above (same urlconf object, no duplication).
    path("api/shipping/", include("shipping.urls")),

    path("api/accounts/", include("accounts.urls")),

    # /api/v1/ namespace (spec §9, R-9.0): same urlconf objects as the
    # legacy mounts above; legacy paths remain alive as aliases.
    path("api/v1/", include((v1_urlpatterns, "v1"), namespace="v1")),
]


# SPEC-2-04 [V-13]: media is served here, unconditionally. Django's
# static() helper returns an empty list when DEBUG=False, so every uploaded
# product image used to 404 in production with nothing logged anywhere - the
# helper is gone from this file and MEDIA is routed explicitly against
# MEDIA_ROOT, which is env-driven (DJANGO_MEDIA_ROOT) to the mounted volume a
# deployment actually persists. STATIC needs no route here: whitenoise
# middleware serves STATIC_ROOT from the app process (SPEC-22-01).
#
# Production front door: nginx (or whatever terminates in front of gunicorn)
# should serve /media/ straight off that same mounted volume rather than
# proxying bytes through Python - see docs/deploy-runbook.md ("Serving
# media"). This route is the Django-side guarantee behind that arrangement:
# a missing or misconfigured front-door rule then degrades to a file served
# by the app instead of a 404, which is why the pattern is NOT gated on
# DEBUG. It is deliberately last in urlpatterns so an application route can
# never be shadowed by a media path.
urlpatterns += [
    re_path(
        r'^' + re.escape(settings.MEDIA_URL.lstrip('/')) + r'(?P<path>.*)$',
        serve_media,
        {'document_root': str(settings.MEDIA_ROOT)},
    )
]
