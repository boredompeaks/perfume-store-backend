from django.urls import path
from .restock_views import restock_notification
from .views import inventory_adjust, product_list, product_detail


urlpatterns = [
    path('', product_list, name='product-list'),
    # SPEC-9-06 [R-9.4.7]: POST-only inventory adjustments (staff-gated by
    # HasInventoryAdjust). Registered before the slug catchall so the
    # literal path can never be swallowed by it.
    path('inventory/adjustments/', inventory_adjust, name='inventory-adjust'),
    # SPEC-19-4 [R-19.11]: customer back-in-stock opt-in/opt-out. Also
    # registered before the slug catchall (the literal suffix outranks
    # the conversion in Django's matcher order here).
    path(
        '<slug:slug>/restock-notifications/',
        restock_notification,
        name='restock-notification',
    ),
    path('<slug:slug>/', product_detail, name='product-detail'),
]