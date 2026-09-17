from django.contrib import admin

from .models import Order, OrderItem, Coupon

@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):

    list_display = (
        'code',
        'discount_type',
        'discount_value',
        'minimum_order_amount',
        'active',
        'valid_from',
        'valid_until',
        'usage_limit',
        'used_count',
    )

    list_filter = (
        'active',
        'discount_type',
    )

    search_fields = (
        'code',
    )

    ordering = (
        '-created_at',
    )

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):

    list_display = (
        'id',
        'user',
        'status',
        'total_amount',
        'created_at',
    )

    list_filter = (
        'status',
        'created_at',
    )

    search_fields = (
        'user__username',
        'full_name',
        'phone',
    )


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):

    list_display = (
        'order',
        'product',
        'quantity',
        'price',
        'subtotal',
    )