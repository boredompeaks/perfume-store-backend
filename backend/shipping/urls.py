from django.urls import path

from .views import ShippingEstimateView

urlpatterns = [
    # Spec 9.1 line 2703, `/store/shipping/estimate`. The mount itself is
    # done twice by config/urls.py (once under /api/v1/store/shipping/, once
    # as the legacy /api/shipping/ alias) over this one urlconf, the same
    # dual-mount rule the products/cart/orders families follow.
    path(
        "estimate/",
        ShippingEstimateView.as_view(),
        name="shipping-estimate",
    ),
]
