from decimal import Decimal

from django.contrib.admin.models import LogEntry
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import render

from common.permissions import capability_required
from . import alerts
from .services import (
    check_stock_alerts,
    get_health,
    get_sales_series,
    get_stats,
)

# Page size for the audit-log table: a presentation constant for an
# internal staff surface, not deployment config — named here so the route
# has no magic numbers.
AUDIT_PAGE_SIZE = 50


def health(request):
    """Public health endpoint — cheap checks, no network calls."""
    health = get_health()
    # [SPEC-19-2] Integration-outage alert ([R-19.20] alerting half) beside
    # the existing detection: a degraded probe notifies the staff mailbox
    # once per cooldown (the probe is polled, so the rule protects the
    # inbox). The dispatch is log-only on failure and cannot alter the
    # response — the monitor must never become the outage it reports.
    if health["status"] != "ok":
        # Best-effort detail: get_health's shape is its own contract (the
        # error-envelope seam may substitute arbitrary 503 bodies), so the
        # alert renders whatever keys are present instead of assuming
        # `checks` — an alert must never 500 the probe that tripped it.
        checks = health.get("checks") or {}
        checks_text = " ".join(f"{key}={value}" for key, value in checks.items())
        alerts.notify_integration_outage(
            f"status={health.get('status')} {checks_text}".rstrip()
        )
    return JsonResponse(health, status=200 if health["status"] == "ok" else 503)


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


@capability_required("reports.read")
def dashboard(request):
    """Store dashboard (spec 5 / route /admin/dashboard): revenue, order and
    inventory aggregates. SPEC-17-10: gated by ``reports.read`` (finance,
    marketing, admin per CAPABILITY_ROLES), not the blanket
    @staff_member_required it replaces — every role on this page reads
    revenue and customer rows, so "any staff account" was never the right
    authority; the sibling audit-log route already used the capability
    decorator and this closes the last blanket-staff chrome route. The
    decorator's contract matches it: anonymous callers are redirected to
    the admin login, unprivileged staff get a visible 403, superusers keep
    their explicit bypass."""
    health = get_health()
    stats = get_stats()
    sales_series = get_sales_series()
    # [SPEC-19-2] The dashboard load is a natural alert poll for the
    # low/out-of-stock breach (same threshold definition the table below
    # renders); log-only + cooldown-deduped, so staff page views cannot
    # mail-bomb anyone.
    check_stock_alerts()

    # Chart helpers for the template: bar heights scale against the busiest
    # day, and the aria summary gives screen readers the real totals (the
    # bars themselves carry no numbers).
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

    # One batched pk -> username fetch replaces the old per-order
    # User.objects.get (N+1 on recent orders). A user row that vanishes
    # between get_stats() and this fetch degrades to the dash, never a 500.
    user_ids = {row["user_id"] for row in stats["recent_orders"]}
    users = User.objects.in_bulk(user_ids)
    recent_orders = []
    for row in stats["recent_orders"]:
        user = users.get(row["user_id"])
        recent_orders.append({**row, "username": user.username if user else "—"})

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
            "sales_series": sales_series,
            "sales_max_revenue": sales_max_revenue,
            "sales_summary": sales_summary,
        },
    )


@capability_required("staff.manage")
def audit_log(request):
    """Audit-log route (spec 6.12, /admin/audit-log): the single staff-gated
    reader for the privileged-action trail. Every admin form save, gated
    bulk action (RoleAwareModelAdmin) and API-side write (log_api_action)
    lands a LogEntry — this page reads that one source.

    Gated by ``staff.manage`` (admin role, the map's only grantee): the
    trail names actors across every domain, including role changes, so
    reading it is privilege-management oversight, not a per-team report.
    """
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
