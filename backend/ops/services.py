"""Shared logic for the admin dashboard and the /health/ endpoint."""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Sum


def _low_stock_threshold() -> int:
    # Read at call time, not import time: env-driven and overridable per test.
    return settings.LOW_STOCK_THRESHOLD


# "Paid" = money actually captured: confirmed/shipped/delivered.
# pending/cancelled never held money.
REVENUE_STATUSES = ("confirmed", "shipped", "delivered")

# Paid orders awaiting shipment; "shipped" is with the carrier and "pending"
# is payment-pending, not fulfilment-pending.
PENDING_FULFILMENT_STATUS = "confirmed"


def get_sales_series(days=None):
    """Daily revenue + order counts over a trailing window of paid orders, zero-filling every day so the chart has no gaps.

    Uses the same REVENUE_STATUSES set as the gross-sales KPI; revenue stays Decimal and is quantized to the paisa.
    """
    from django.db.models.functions import TruncDate
    from django.utils import timezone

    from orders.models import Order

    if days is None:
        # Read at call time, not import time: env-driven and test-overridable.
        days = settings.DASHBOARD_SALES_WINDOW_DAYS
    # A malformed env window must not produce an empty or reversed range.
    days = max(1, int(days))

    today = timezone.localdate()
    start = today - timedelta(days=days - 1)
    rows = (
        Order.objects.filter(
            status__in=REVENUE_STATUSES,
            created_at__date__gte=start,
            created_at__date__lte=today,
        )
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(revenue=Sum("total_amount"), orders=Count("id"))
        .order_by("day")
    )
    by_day = {row["day"]: row for row in rows}
    series = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        row = by_day.get(day)
        series.append(
            {
                "date": day,
                "revenue": (
                    (row["revenue"] or Decimal("0.00")).quantize(Decimal("0.01"))
                    if row
                    else Decimal("0.00")
                ),
                "orders": row["orders"] if row else 0,
            }
        )
    return series


def _media_writable() -> bool:
    try:
        settings.MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
        probe = settings.MEDIA_ROOT / "health_probe.txt"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def _razorpay_mode() -> str:
    key = getattr(settings, "RAZORPAY_KEY_ID", "") or ""
    if key.startswith("rzp_test_"):
        return "test"
    if key.startswith("rzp_live_"):
        return "live"
    return "unset"


def _shipping_configured() -> bool:
    """Whether the store has at least one ACTIVE shipping method; the lazy import keeps the shipping app out of this module's import time."""
    from shipping.pricing import shipping_configured

    return shipping_configured()


def get_health() -> dict:
    """Cheap checks only — no network calls, safe to poll."""
    from cart.models import Cart
    from orders.models import Order
    from products.models import products

    checks = {}
    try:
        pending_orders = Order.objects.filter(status="pending").count()
        carts = Cart.objects.count()
        checks["database"] = True
    except Exception:
        pending_orders = None
        carts = None
        checks["database"] = False

    checks["media_writable"] = _media_writable()
    checks["smtp_configured"] = bool(
        settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD
    )
    checks["razorpay_mode"] = _razorpay_mode()
    # Reported beside the other config checks; deliberately NOT part of
    # `status`: an unconfigured store is pre-launch, not an outage, and a 503
    # would page staff on every poll.
    checks["shipping_configured"] = _shipping_configured()

    low_stock = 0
    out_of_stock = 0
    threshold = _low_stock_threshold()
    if checks["database"]:
        low_stock = products.objects.filter(stock__gt=0, stock__lte=threshold).count()
        out_of_stock = products.objects.filter(stock=0).count()

    return {
        "status": (
            "ok" if checks["database"] and checks["media_writable"] else "degraded"
        ),
        "checks": checks,
        "pending_orders": pending_orders,
        "carts": carts,
        "low_stock": low_stock,
        "out_of_stock": out_of_stock,
        "low_stock_threshold": threshold,
        # The stock alert fires from check_stock_alerts (polled beside
        # /health/), never inside get_health: the monitor must not become the
        # outage.
        "stock_alert_checked": False,
    }


def _alert_rows(queryset):
    """Render low/out-of-stock products as the alert payload dicts."""
    return [{"id": row.id, "name": row.name, "stock": row.stock} for row in queryset]


def check_stock_alerts():
    """Low/out-of-stock breach detection + admin alert, using the same threshold the dashboard renders; dispatch is cooldown-deduped and log-only."""
    from products.models import products

    from .alerts import notify_low_stock, notify_out_of_stock

    threshold = _low_stock_threshold()
    notify_low_stock(
        _alert_rows(products.objects.filter(stock__gt=0, stock__lte=threshold))
    )
    notify_out_of_stock(_alert_rows(products.objects.filter(stock=0)))


def get_stats() -> dict:
    from django.contrib.auth.models import User

    from orders.models import Order
    from products.models import products

    from .models import SiteSettings

    by_status = {status: 0 for status, _ in Order.STATUS_CHOICES}
    for row in Order.objects.values("status").annotate(n=Count("id")):
        by_status[row["status"]] = row["n"]

    paid = Order.objects.filter(status__in=REVENUE_STATUSES).aggregate(
        total=Sum("total_amount"), n=Count("id")
    )
    paid_orders = paid["n"]
    revenue = paid["total"] or 0
    # Quantize to the paisa; guard on the paid count, the only case where the
    # division is invalid.
    average_order_value = (
        (paid["total"] / paid_orders).quantize(Decimal("0.01"))
        if paid_orders
        else Decimal("0.00")
    )

    settings_row = SiteSettings.load()
    return {
        "users": User.objects.count(),
        "products": products.objects.count(),
        "orders_total": sum(by_status.values()),
        "orders_by_status": by_status,
        "average_order_value": str(average_order_value),
        "orders_pending_fulfilment": by_status[PENDING_FULFILMENT_STATUS],
        "revenue": str(revenue),
        "recent_orders": list(
            Order.objects.order_by("-created_at").values(
                "id", "status", "total_amount", "created_at", "user_id"
            )[:10]
        ),
        "support_email": settings_row.support_email,
    }
