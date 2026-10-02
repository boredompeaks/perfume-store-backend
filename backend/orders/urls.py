from django.urls import path

from .views import (
    order_list,
    order_detail,
    create_order,
    apply_coupon,
    create_payment,
    verify_payment,
    # [R-1.13] SPEC-1-B04: the guest's own order, keyed on the token minted
    # at checkout rather than on a session.
    guest_order_detail,
    # [R-1.16] SPEC-1-B07a/B07b: the customer's returns family - POST creates
    # one, GET lists the caller's own, and the keyed route reads one. The body
    # of the create names the order, so it is a POST against the family root
    # rather than a keyed detail route.
    return_requests,
    return_request_detail,
)


urlpatterns = [

    path(
        '',
        order_list,
        name='order-list'
    ),

    path(
        '<int:order_id>/',
        order_detail,
        name='order-detail'
    ),

    path(
        'checkout/',
        create_order,
        name='create-order'
    ),

    # [R-1.13] Guest order read, two shapes of the same view: the token alone
    # (it is globally unique, so it identifies the order) and the token plus
    # the customer-facing order number as an extra cross-check. Both take the
    # token in the X-Guest-Order-Token header, never in the path or query.
    # Declared after the int route above, which cannot match "guest" anyway,
    # so the two never compete.
    path("guest/", guest_order_detail, name="guest-order-detail"),
    path(
        "guest/<str:order_number>/",
        guest_order_detail,
        name="guest-order-detail-by-number",
    ),

    path(
        'apply-coupon/',
        apply_coupon,
        name='apply-coupon'
    ),

    path(
        'payment/',
        create_payment,
        name='create-payment'
    ),

    path(
    'payment/verify/',
    verify_payment,
    name='verify-payment'
),

    # [R-1.16] SPEC-1-B07a/B07b: the customer's returns family (spec 4 line 1083,
    # under the account's `/account/returns` page of line 1045). Declared after
    # the `<int:order_id>` route above, which cannot match a non-integer, so
    # the two never compete. Mounted in both families by config/urls.py because
    # this urlconf is already included under store/orders/ and api/orders/.
    #
    # ONE view for the family root, not one per method: Django resolves the
    # FIRST matching pattern, so two patterns on "returns/" would leave the
    # second permanently unreachable - a listing that answers 405 forever. The
    # view dispatches on the request method instead, which is also what keeps
    # B07a's POST contract intact and unmodified behind a GET the storefront
    # needs. The detail route is a separate pattern one level deeper.
    path("returns/", return_requests, name="return-requests"),
    path(
        "returns/<int:return_request_id>/",
        return_request_detail,
        name="return-request-detail",
    ),
]
