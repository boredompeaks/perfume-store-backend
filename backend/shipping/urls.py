from django.urls import path

from .views import ShipmentTrackingView, ShippingEstimateView

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
    # Spec 3.9 line 1185, `/track-order` - the customer-facing tracking page's
    # data source, line 1189's "secure, limited-access token or authenticated
    # account". Keyed on the CUSTOMER-FACING order number (never the pk), and
    # the number alone authorizes nothing (shipping.views._trackable_shipments).
    path(
        "track/<str:order_number>/",
        ShipmentTrackingView.as_view(),
        name="shipment-tracking",
    ),
]
