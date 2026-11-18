from django.urls import path

from .views import ShipmentTrackingView, ShippingEstimateView

urlpatterns = [
    # Mounted twice by config/urls.py (v1 store path plus the legacy alias),
    # the same dual-mount rule the other families follow.
    path(
        "estimate/",
        ShippingEstimateView.as_view(),
        name="shipping-estimate",
    ),
    # Keyed on the customer-facing order number, never the pk; the number
    # alone authorizes nothing (see shipping.views._trackable_shipments).
    path(
        "track/<str:order_number>/",
        ShipmentTrackingView.as_view(),
        name="shipment-tracking",
    ),
]
