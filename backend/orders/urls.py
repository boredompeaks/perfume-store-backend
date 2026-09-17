from django.urls import path

from .views import (
    order_list,
    create_order,
    apply_coupon,
    create_payment,
    verify_payment
)


urlpatterns = [

    path(
        '',
        order_list,
        name='order-list'
    ),

    path(
        'checkout/',
        create_order,
        name='create-order'
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