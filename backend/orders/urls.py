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
    # [R-1.16] SPEC-1-B07a: the customer asks to send an order back. The body
    # names the order, so this is a POST against the family rather than a
    # keyed detail route.
    return_request_create,
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

    # [R-1.16] SPEC-1-B07a: the customer's return request (spec 4 line 1083,
    # under the account's `/account/returns` page of line 1045). Declared after
    # the `<int:order_id>` route above, which cannot match a non-integer, so
    # the two never compete. Mounted in both families by config/urls.py because
    # this urlconf is already included under store/orders/ and api/orders/.
    path("returns/", return_request_create, name="return-request-create"),
]
