import re

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve as serve_media

from common.admin_search import global_search
from common.onboarding import onboarding
from common.saved_filters import delete_saved_filter, save_saved_filter
from ops.views import api_settings, audit_log, dashboard, health, health_detail
from orders.views import (
    admin_order_cancel,
    admin_order_detail,
    admin_order_fulfill,
    admin_order_list,
    admin_order_refund,
)

# The webhook authenticates no user: its only credential is the provider's
# signature over the raw request body.
from orders.webhooks import razorpay_webhook
from products.views import inventory_adjust

# The v1 mounts reuse the legacy urlconf objects (no view duplication); legacy
# /api/ paths stay as aliases until the frontend base-URL cutover.

v1_store_patterns = [
    path("products/", include("products.urls")),
    path("cart/", include("cart.urls")),
    path("orders/", include("orders.urls")),
    path("shipping/", include("shipping.urls")),
    path("config/", api_settings, name="config"),
]

v1_account_patterns = [
    path("", include("accounts.urls")),
]

v1_admin_patterns = [
    path("dashboard/", dashboard, name="dashboard"),
    path("audit-log/", audit_log, name="audit-log"),
    path("health/", health_detail, name="health"),
    path("orders/", admin_order_list, name="orders-list"),
    path("orders/<int:order_id>/", admin_order_detail, name="orders-detail"),
    path("orders/<int:order_id>/fulfill/", admin_order_fulfill, name="orders-fulfill"),
    path("orders/<int:order_id>/cancel/", admin_order_cancel, name="orders-cancel"),
    path("orders/<int:order_id>/refund/", admin_order_refund, name="orders-refund"),
    path(
        "inventory/adjustments/",
        inventory_adjust,
        name="inventory-adjust",
    ),
]

# Named by provider so a second payment provider can be added without
# re-deciding the mount.
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
    # These chrome routes must be registered BEFORE admin.site.urls, or the
    # admin URLconf swallows them.
    path("admin/dashboard/", dashboard, name="admin-dashboard"),
    path("admin/audit-log/", audit_log, name="admin-audit-log"),
    path("admin/search/", global_search, name="admin-global-search"),
    path("admin/onboarding/", onboarding, name="admin-onboarding"),
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
    path(
        "api/admin/orders/<int:order_id>/refund/",
        admin_order_refund,
        name="admin-orders-refund",
    ),
    path(
        "api/admin/inventory/adjustments/",
        inventory_adjust,
        name="admin-inventory-adjust",
    ),
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    path("api/settings/", api_settings, name="api-settings"),
    path("api/products/", include("products.urls")),
    path("api/cart/", include("cart.urls")),
    path("api/orders/", include("orders.urls")),
    path("api/shipping/", include("shipping.urls")),
    path("api/accounts/", include("accounts.urls")),
    path("api/v1/", include((v1_urlpatterns, "v1"), namespace="v1")),
]


# Django's static() helper returns [] when DEBUG=False, so MEDIA is routed
# explicitly; kept last so an application route is never shadowed.
urlpatterns += [
    re_path(
        r"^" + re.escape(settings.MEDIA_URL.lstrip("/")) + r"(?P<path>.*)$",
        serve_media,
        {"document_root": str(settings.MEDIA_ROOT)},
    )
]
