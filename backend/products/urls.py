from django.urls import path

from .restock_views import restock_notification
from .views import inventory_adjust, product_detail, product_list

urlpatterns = [
    path("", product_list, name="product-list"),
    # Deprecated alias kept reachable for existing clients; registered before
    # the slug catchall so the literal path is not swallowed.
    path("inventory/adjustments/", inventory_adjust, name="inventory-adjust"),
    # Registered before the slug catchall so the literal suffix wins.
    path(
        "<slug:slug>/restock-notifications/",
        restock_notification,
        name="restock-notification",
    ),
    path("<slug:slug>/", product_detail, name="product-detail"),
]
