from django.urls import path

from .restock_views import restock_notification
from .views import inventory_adjust, product_detail, product_list

urlpatterns = [
    path("", product_list, name="product-list"),
    # DEPRECATED ALIAS (SPEC-9-T3, R-9.4.7): the canonical inventory
    # adjustment write is now mounted in the admin family (config/urls.py,
    # /api/v1/admin/ with the legacy /api/admin/ alias). This store-family
    # path, and the /api/v1/store/ mirror it is included under, stay
    # reachable for existing clients and resolve to the same view object.
    # Registered before the slug catchall so the literal path can never be
    # swallowed by it.
    path("inventory/adjustments/", inventory_adjust, name="inventory-adjust"),
    # SPEC-19-4 [R-19.11]: customer back-in-stock opt-in/opt-out. Also
    # registered before the slug catchall (the literal suffix outranks
    # the conversion in Django's matcher order here).
    path(
        "<slug:slug>/restock-notifications/",
        restock_notification,
        name="restock-notification",
    ),
    path("<slug:slug>/", product_detail, name="product-detail"),
]
