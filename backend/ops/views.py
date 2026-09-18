from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import render

from .services import get_health, get_stats


def health(request):
    """Public health endpoint — cheap checks, no network calls."""
    health = get_health()
    status_code = 200 if health["status"] == "ok" else 503
    return JsonResponse(health, status=status_code)


def api_settings(request):
    """Public store settings consumed by the frontend (contact channels)."""
    from .models import SiteSettings

    row = SiteSettings.load()
    return JsonResponse(
        {
            "support_email": row.support_email,
            "support_phone": row.support_phone,
            "whatsapp_number": row.whatsapp_number,
            "whatsapp_message": row.whatsapp_message,
            "instagram_url": row.instagram_url,
        }
    )


@staff_member_required
def dashboard(request):
    health = get_health()
    stats = get_stats()

    from django.contrib.auth.models import User

    from orders.models import Order
    from products.models import StockMovement, products

    recent_orders = []
    for row in stats["recent_orders"]:
        try:
            user = User.objects.get(pk=row["user_id"])
            username = user.username
        except User.DoesNotExist:
            username = "—"
        recent_orders.append({**row, "username": username})

    recent_movements = StockMovement.objects.select_related(
        "product", "created_by"
    )[:10]

    low_stock_items = products.objects.filter(
        stock__gt=0, stock__lte=health["low_stock_threshold"]
    ).order_by("stock")[:10]
    out_of_stock_items = products.objects.filter(stock=0).order_by("name")[:10]

    return render(
        request,
        "ops/dashboard.html",
        {
            "title": "Store dashboard",
            "health": health,
            "stats": stats,
            "recent_orders": recent_orders,
            "recent_movements": recent_movements,
            "low_stock_items": low_stock_items,
            "out_of_stock_items": out_of_stock_items,
            "status_choices": Order.STATUS_CHOICES,
        },
    )
