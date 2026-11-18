from django.urls import path

from .views import (
    apply_coupon,
    create_order,
    create_payment,
    # The guest's own order, keyed on a token rather than a session.
    guest_order_detail,
    order_detail,
    order_list,
    refund_request,
    return_request_detail,
    return_requests,
    verify_payment,
)

urlpatterns = [
    path("", order_list, name="order-list"),
    path("<int:order_id>/", order_detail, name="order-detail"),
    # Mounted under both the store/ and api/ families by config/urls.py; the
    # view owns the uniform 404 for a foreign order.
    path(
        "<int:order_id>/refund-request/",
        refund_request,
        name="refund-request",
    ),
    path("checkout/", create_order, name="create-order"),
    # Guest reads take the globally unique token in the X-Guest-Order-Token
    # header, never the path or query; the number is only an extra cross-check.
    path("guest/", guest_order_detail, name="guest-order-detail"),
    path(
        "guest/<str:order_number>/",
        guest_order_detail,
        name="guest-order-detail-by-number",
    ),
    path("apply-coupon/", apply_coupon, name="apply-coupon"),
    path("payment/", create_payment, name="create-payment"),
    path("payment/verify/", verify_payment, name="verify-payment"),
    # One view for the family root: two patterns on "returns/" would leave the
    # second permanently unreachable (a listing that answers 405 forever), so
    # the view dispatches on the request method instead.
    path("returns/", return_requests, name="return-requests"),
    path(
        "returns/<int:return_request_id>/",
        return_request_detail,
        name="return-request-detail",
    ),
]
