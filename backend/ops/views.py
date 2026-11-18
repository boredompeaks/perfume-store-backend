from decimal import Decimal

from django.contrib.admin.models import LogEntry
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import render
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from common.permissions import HasReportsRead, capability_required

from . import alerts
from .alerts import check_payment_failure_spike
from .services import (
    check_stock_alerts,
    get_health,
    get_sales_series,
    get_stats,
)

# Page size for the audit-log table (presentation constant, not deployment
# config).
AUDIT_PAGE_SIZE = 50

# What the anonymous probe may carry: release waiters gate on the HTTP status
# alone, so the body needs only the checks that drive it plus the pre-launch
# shipping marker. A whitelist, not a blacklist.
PUBLIC_HEALTH_CHECKS = ("database", "media_writable", "shipping_configured")

# Top-level keys /health/ strips; get_health() still computes them for the
# dashboard and health_detail.
STAFF_ONLY_HEALTH_KEYS = frozenset(
    {
        "pending_orders",
        "carts",
        "low_stock",
        "out_of_stock",
        "low_stock_threshold",
        "stock_alert_checked",
    }
)


def _public_health(health):
    """Project get_health()'s full payload onto the public contract.

    Subtract by name rather than rebuilding: the error-envelope seam substitutes arbitrary 503 bodies, and dropping unknown keys would swallow the envelope the middleware is about to wrap.
    """
    body = {k: v for k, v in health.items() if k not in STAFF_ONLY_HEALTH_KEYS}
    checks = body.get("checks")
    if checks is not None:
        body["checks"] = {k: v for k, v in checks.items() if k in PUBLIC_HEALTH_CHECKS}
    return body


def health(request):
    """Public health endpoint: deploy-relevant checks only, no network calls; 200 ok / 503 degraded is the contract release waiters poll."""
    health = get_health()
    # Integration-outage alert beside the existing detection; log-only
    # dispatch cannot alter the response.
    if health["status"] != "ok":
        # Best-effort detail: render whatever keys are present rather than
        # assuming `checks`, so an alert never 500s the probe that tripped it.
        # The full payload is deliberate: the mailbox is the staff surface.
        checks = health.get("checks") or {}
        checks_text = " ".join(f"{key}={value}" for key, value in checks.items())
        alerts.notify_integration_outage(
            f"status={health.get('status')} {checks_text}".rstrip()
        )
    return JsonResponse(
        _public_health(health), status=200 if health["status"] == "ok" else 503
    )


@api_view(["GET"])
@permission_classes([HasReportsRead])
def health_detail(request):
    """GET /api/v1/admin/health/ — the full probe payload behind ``reports.read``, the same capability that gates the dashboard."""
    return Response(get_health())


def _absolute_media_url(request, file_field):
    """The public absolute URL of an uploaded media file, or None when unset. Absolute because the storefront is on a different origin; None because "" is indistinguishable from a broken path."""
    if not file_field:
        return None
    return request.build_absolute_uri(file_field.url)


def api_settings(request):
    """Public store settings consumed by the frontend (contact channels)."""
    from .models import SiteSettings

    row = SiteSettings.load()
    return JsonResponse(
        {
            # Keys are listed explicitly, never a model dump, so a future
            # column is not disclosed by accident.
            "name": row.name,
            "logo": _absolute_media_url(request, row.logo),
            "locale": row.locale,
            "maintenance_mode": row.maintenance_mode,
            "support_email": row.support_email,
            "support_phone": row.support_phone,
            "whatsapp_number": row.whatsapp_number,
            "whatsapp_message": row.whatsapp_message,
            "instagram_url": row.instagram_url,
            # The RESOLVED window, not the raw column, so the storefront never
            # keeps its own copy of the default.
            "return_window_days": row.resolved_return_window_days(),
        }
    )


@capability_required("reports.read")
def dashboard(request):
    """Store dashboard: revenue, order and inventory aggregates, gated by ``reports.read`` (finance, marketing, admin) rather than blanket staff access."""
    health = get_health()
    stats = get_stats()
    sales_series = get_sales_series()
    # The dashboard load is a natural alert poll for stock breaches; log-only
    # and cooldown-deduped, so page views cannot mail-bomb anyone.
    check_stock_alerts()
    # The payment-failure spike detector's production call site; its audit
    # query is skipped when ALERT_RECIPIENTS is empty.
    check_payment_failure_spike()

    # Chart helpers: bar heights scale against the busiest day, and the aria
    # summary gives screen readers the real totals.
    sales_max_revenue = max(day["revenue"] for day in sales_series)
    sales_total_revenue = sum((day["revenue"] for day in sales_series), Decimal("0.00"))
    sales_total_orders = sum(day["orders"] for day in sales_series)
    sales_summary = (
        f"Sales over time for the last {len(sales_series)} days: "
        f"{sales_total_orders} paid orders, total revenue ₹{sales_total_revenue}"
    )

    from django.contrib.auth.models import User

    from orders.models import Order
    from products.models import StockMovement, products

    # One batched pk -> username fetch (no N+1); a user that vanishes between
    # get_stats() and here degrades to a dash, never a 500.
    user_ids = {row["user_id"] for row in stats["recent_orders"]}
    users = User.objects.in_bulk(user_ids)
    recent_orders = []
    for row in stats["recent_orders"]:
        user = users.get(row["user_id"])
        recent_orders.append({**row, "username": user.username if user else "—"})

    recent_movements = StockMovement.objects.select_related("product", "created_by")[
        :10
    ]

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
            "sales_series": sales_series,
            "sales_max_revenue": sales_max_revenue,
            "sales_summary": sales_summary,
        },
    )


@capability_required("staff.manage")
def audit_log(request):
    """Audit-log route: the single staff-gated reader for the privileged-action trail, behind ``staff.manage`` (admin only) because it names actors across every domain."""
    entries = LogEntry.objects.select_related("user", "content_type").order_by(
        "-action_time"
    )
    paginator = Paginator(entries, AUDIT_PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "ops/audit_log.html",
        {"title": "Audit log", "entries": page},
    )
