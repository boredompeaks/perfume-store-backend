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

]
