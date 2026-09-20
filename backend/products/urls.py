from django.urls import path
from .views import inventory_adjust, product_list, product_detail


urlpatterns = [
    path('', product_list, name='product-list'),
    # SPEC-9-06 [R-9.4.7]: POST-only inventory adjustments (staff-gated by
    # HasInventoryAdjust). Registered before the slug catchall so the
    # literal path can never be swallowed by it.
    path('inventory/adjustments/', inventory_adjust, name='inventory-adjust'),
    path('<slug:slug>/', product_detail, name='product-detail'),
]