"""Read-only local-vs-gateway payment reconciliation (NEW-P1-1a).

The store records what the gateway confirmed, but a dropped callback, a
hand-edited row or a capture that never reached the order leaves the two
sides disagreeing with no error anywhere. This command compares them over a
bounded window and raises one admin alert per divergence class; it never
writes an order, payment, refund or stock row, because a detector that
"fixes" the money it is inspecting destroys the evidence the operator needs
and can double-move funds during the very incident it was built for.

Four classes are checked:

- a local capture the gateway has no captured payment for (including a
  capture with no ``razorpay_payment_id`` at all);
- a gateway capture in the window with no locally captured order;
- an amount/currency disagreement on a payment both sides know;
- a refund disagreement in either direction (a processed local refund with
  no gateway reference, a local refund the gateway cannot find, a gateway
  refund with no local row, or a refund amount/currency mismatch).

Window bounds default to the last 24 hours. Local orders are selected by
``paid_at`` and local refunds by ``created_at``; the gateway listings use
the same instants, while a payment/refund named by a local row is fetched
by id so a row created just outside the gateway listing window still
reconciles. A gateway read that fails for any reason other than "this
object does not exist" aborts the run with a non-zero exit and no
divergence alert: a partial read is not a reconciliation.

Exit status is non-zero when at least one divergence is found and zero when
the window is clean. ``--dry-run`` is deliberately absent - the command has
no state to protect itself from.
"""

import logging
from datetime import datetime, timedelta

import razorpay
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from common.money import quantize_money
from ops import alerts
from orders.models import Order, Refund
from orders.state import CAPTURED_SALE_PAYMENT_STATUSES, PAYMENT_METHOD_COD

logger = logging.getLogger("ops.reconcile_payments")

DEFAULT_WINDOW_HOURS = 24
# Bounded read: a busy window is paginated, and a listing that exceeds the
# cap is refused rather than silently truncated, because a missing page reads
# exactly like a clean window for the gateway-only class.
PAGE_SIZE = 100
MAX_PAGES = 50

GATEWAY_CAPTURED = "captured"

LOCAL_CAPTURE_WITHOUT_GATEWAY = "local_capture_without_gateway"
GATEWAY_CAPTURE_WITHOUT_LOCAL = "gateway_capture_without_local"
AMOUNT_CURRENCY_MISMATCH = "amount_currency_mismatch"
REFUND_MISMATCH = "refund_mismatch"

# Declaration order is also report order, so an operator reading the summary
# sees the classes in the same order every run.
DIVERGENCE_LABELS = {
    LOCAL_CAPTURE_WITHOUT_GATEWAY: "local capture without gateway payment",
    GATEWAY_CAPTURE_WITHOUT_LOCAL: "gateway capture without local captured order",
    AMOUNT_CURRENCY_MISMATCH: "amount or currency mismatch",
    REFUND_MISMATCH: "refund mismatch",
}


def _minor_units(amount):
    """``Decimal('10.00') -> 1000``: the provider's smallest currency unit.

    Quantized first, exactly like the capture and refund writers, so the
    reconciliation compares the same rounding the money actually moved under.
    """
    return int(quantize_money(amount) * 100)


def _mismatch(local_amount, local_currency, gateway):
    """Describe an amount/currency disagreement, or return ``None``.

    A gateway amount that is not an ``int`` is a disagreement rather than a
    crash: the provider's documented shape is integer minor units, and a
    string or float there means the comparison cannot be trusted, which is
    precisely what an operator needs to hear.
    """
    gateway_amount = gateway.get("amount")
    if isinstance(gateway_amount, bool) or not isinstance(gateway_amount, int):
        return f"gateway amount {gateway_amount!r} is not integer minor units"
    expected = _minor_units(local_amount)
    if gateway_amount != expected:
        return (
            f"amount local {quantize_money(local_amount)} {local_currency} "
            f"({expected} minor units) vs gateway {gateway_amount} minor units"
        )
    gateway_currency = str(gateway.get("currency") or "")
    if gateway_currency.upper() != str(local_currency).upper():
        return (
            f"currency local {local_currency} vs gateway "
            f"{gateway_currency or '(missing)'}"
        )
    return None


def _payment_description(gateway):
    return (
        f"gateway payment {gateway['id']} "
        f"(order {gateway.get('order_id') or '(none)'}, "
        f"amount {gateway.get('amount')} {gateway.get('currency') or '(no currency)'})"
    )


class Command(BaseCommand):
    help = (
        "Read-only reconciliation of local paid orders and processed refunds "
        "against the Razorpay gateway over a bounded window (default: last "
        "24 hours). Detects local captures the gateway does not have, gateway "
        "captures with no locally captured order, amount/currency mismatches "
        "and refund disagreements; alerts once per divergence class and exits "
        "non-zero when any is found. Never writes business state."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--since",
            default=None,
            help="Window start as an ISO-8601 datetime (default: 24h before --until).",
        )
        parser.add_argument(
            "--until",
            default=None,
            help="Window end as an ISO-8601 datetime (default: now).",
        )

    def handle(self, *args, **options):
        since, until = self._window(options)
        if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
            raise CommandError(
                "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be configured "
                "to reconcile payments with the gateway."
            )
        client = razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )

        # Collection completes before any alert is emitted: a gateway failure
        # half-way through must surface as one loud CommandError, not as a
        # partial set of divergence mails that reads like a finished sweep.
        divergences = self._collect(client, since, until)
        self._emit_alerts(divergences, since, until)

        total = sum(len(rows) for rows in divergences.values())
        if options.get("verbosity", 1) >= 1:
            self.stdout.write(
                f"reconcile_payments: window {since.isoformat()} .. {until.isoformat()}"
            )
            for key in DIVERGENCE_LABELS:
                if divergences[key]:
                    self.stdout.write(f"- {key}: {len(divergences[key])}")
            if not total:
                self.stdout.write("reconcile_payments: no divergences found.")
        if total:
            logger.warning(
                "payment reconciliation found %d divergence(s) in %s .. %s",
                total,
                since.isoformat(),
                until.isoformat(),
            )
            raise CommandError(
                f"{total} payment reconciliation divergence(s) found over "
                f"{since.isoformat()} .. {until.isoformat()}; see the per-class "
                "alert(s) for the rows."
            )

    def _window(self, options):
        until = (
            self._bound(options.get("until"), "--until")
            if options.get("until")
            else timezone.now()
        )
        since = (
            self._bound(options.get("since"), "--since")
            if options.get("since")
            else until - timedelta(hours=DEFAULT_WINDOW_HOURS)
        )
        if since >= until:
            raise CommandError("--since must be earlier than --until")
        return since, until

    @staticmethod
    def _bound(raw, option):
        """Parse one window bound; a naive value takes the current timezone."""
        if isinstance(raw, datetime):
            value = raw
        else:
            value = parse_datetime(str(raw))
            if value is None:
                raise CommandError(f"{option} must be an ISO-8601 datetime")
        if timezone.is_naive(value):
            value = timezone.make_aware(value, timezone.get_current_timezone())
        return value

    def _collect(self, client, since, until):
        divergences = {key: [] for key in DIVERGENCE_LABELS}

        local_orders = list(
            Order.objects.filter(
                paid_at__gte=since,
                paid_at__lte=until,
                # The machine's "a capture committed this sale" set, not a
                # local re-listing: it includes "refunded", which is still a
                # capture the gateway must account for.
                payment_status__in=CAPTURED_SALE_PAYMENT_STATUSES,
            ).exclude(payment_method=PAYMENT_METHOD_COD)
        )
        gateway_payments = self._pages(client.payment.all, since, until, "payments")
        for order in local_orders:
            self._check_local_capture(client, order, divergences)
        self._check_gateway_captures(gateway_payments, divergences)
        self._check_refunds(client, since, until, divergences)
        return divergences

    def _check_local_capture(self, client, order, divergences):
        reference = order.razorpay_payment_id
        if not reference:
            # A COD order never has a gateway payment; it is excluded from the
            # local set above precisely so this branch means "prepaid money
            # recorded with no provider reference", which is a divergence.
            divergences[LOCAL_CAPTURE_WITHOUT_GATEWAY].append(
                f"order #{order.id} is {order.payment_status} locally but has "
                "no razorpay_payment_id"
            )
            return
        payment = self._fetch(client.payment.fetch, reference, "payment")
        if payment is None:
            divergences[LOCAL_CAPTURE_WITHOUT_GATEWAY].append(
                f"order #{order.id} is {order.payment_status} locally; gateway "
                f"has no payment {reference}"
            )
            return
        if payment.get("status") != GATEWAY_CAPTURED:
            divergences[LOCAL_CAPTURE_WITHOUT_GATEWAY].append(
                f"order #{order.id} is {order.payment_status} locally; gateway "
                f"payment {reference} is {payment.get('status')!r}"
            )
            return
        mismatch = _mismatch(order.total_amount, order.currency, payment)
        if mismatch:
            divergences[AMOUNT_CURRENCY_MISMATCH].append(
                f"order #{order.id} payment {reference}: {mismatch}"
            )

    def _check_gateway_captures(self, gateway_payments, divergences):
        captured = [p for p in gateway_payments if p.get("status") == GATEWAY_CAPTURED]
        payment_ids = [p["id"] for p in captured]
        gateway_order_ids = [p["order_id"] for p in captured if p.get("order_id")]

        # Joined against ALL local orders, not just the window's: a capture
        # whose local order was paid before the window is reconciled, not
        # reported as an orphan.
        local_by_payment = (
            {
                order.razorpay_payment_id: order
                for order in Order.objects.filter(razorpay_payment_id__in=payment_ids)
            }
            if payment_ids
            else {}
        )
        local_by_order = (
            {
                order.razorpay_order_id: order
                for order in Order.objects.filter(
                    razorpay_order_id__in=gateway_order_ids
                )
            }
            if gateway_order_ids
            else {}
        )

        for payment in captured:
            local = local_by_payment.get(payment.get("id")) or local_by_order.get(
                payment.get("order_id")
            )
            description = _payment_description(payment)
            if local is None:
                divergences[GATEWAY_CAPTURE_WITHOUT_LOCAL].append(
                    f"{description} has no local order"
                )
                continue
            if (
                local.payment_method == PAYMENT_METHOD_COD
                or local.payment_status not in CAPTURED_SALE_PAYMENT_STATUSES
            ):
                divergences[GATEWAY_CAPTURE_WITHOUT_LOCAL].append(
                    f"{description} matches order #{local.id} which is not "
                    f"locally captured (payment_status={local.payment_status!r})"
                )

    def _check_refunds(self, client, since, until, divergences):
        local_refunds = list(
            Refund.objects.select_related("order").filter(
                created_at__gte=since,
                created_at__lte=until,
            )
        )
        gateway_refunds = self._pages(client.refund.all, since, until, "refunds")
        # Every gateway refund id the store has ever recorded, not only this
        # window's: the reverse check asks whether a gateway refund is known
        # locally at all, and a row created before the window is still known.
        local_gateway_ids = set(
            Refund.objects.exclude(gateway_refund_id__isnull=True)
            .exclude(gateway_refund_id="")
            .values_list("gateway_refund_id", flat=True)
        )

        for refund in local_refunds:
            if not refund.gateway_refund_id:
                if refund.status == Refund.Status.PROCESSED:
                    divergences[REFUND_MISMATCH].append(
                        f"refund #{refund.id} (order #{refund.order_id}, "
                        f"{refund.amount}) is processed locally with no gateway "
                        "refund id"
                    )
                continue
            gateway = self._fetch(
                client.refund.fetch, refund.gateway_refund_id, "refund"
            )
            if gateway is None:
                divergences[REFUND_MISMATCH].append(
                    f"refund #{refund.id} (order #{refund.order_id}, "
                    f"{refund.amount}) names gateway refund "
                    f"{refund.gateway_refund_id} which the gateway does not have"
                )
                continue
            mismatch = _mismatch(refund.amount, refund.order.currency, gateway)
            if mismatch:
                divergences[REFUND_MISMATCH].append(
                    f"refund #{refund.id} (order #{refund.order_id}) gateway "
                    f"{refund.gateway_refund_id}: {mismatch}"
                )

        for gateway in gateway_refunds:
            refund_id = gateway["id"]
            if refund_id not in local_gateway_ids:
                divergences[REFUND_MISMATCH].append(
                    f"gateway refund {refund_id} (payment "
                    f"{gateway.get('payment_id') or '(none)'}, amount "
                    f"{gateway.get('amount')} {gateway.get('currency') or '(none)'}) "
                    "has no local refund row"
                )

    def _pages(self, method, since, until, what):
        """Every item in a gateway collection listing, or a refusal.

        A short page ends the loop because that is the provider's own
        end-of-collection signal. A full page at the cap raises instead of
        returning what was read: the gateway-only class is detected from this
        listing, so a truncated one would report a clean window for every
        capture it never saw.
        """
        params = {
            "from": int(since.timestamp()),
            "to": int(until.timestamp()),
        }
        items = []
        for page in range(MAX_PAGES):
            page_params = dict(params, count=PAGE_SIZE, skip=page * PAGE_SIZE)
            response = self._gateway_call(
                method, page_params, f"{what} listing (page {page + 1})"
            )
            batch = response.get("items") if isinstance(response, dict) else None
            if not isinstance(batch, list):
                raise CommandError(
                    f"gateway {what} listing carried no items list; refusing to "
                    "reconcile an unreadable response"
                )
            for item in batch:
                # Both listings are keyed on ``id`` by the provider's schema.
                # An item without one cannot be joined to a local row, so it is
                # refused rather than skipped - a silent skip is indistinguishable
                # from a clean reconciliation for that item.
                if not isinstance(item, dict) or not item.get("id"):
                    raise CommandError(
                        f"gateway {what} listing carried an item without an id; "
                        "refusing to reconcile it"
                    )
            items.extend(batch)
            if len(batch) < PAGE_SIZE:
                return items
        raise CommandError(
            f"gateway {what} listing exceeded {MAX_PAGES} page(s) for the "
            "window; refusing to reconcile a partial listing"
        )

    @staticmethod
    def _gateway_call(method, params, what):
        # Re-raised as CommandError rather than swallowed: an unreachable or
        # failing gateway means the window was NOT reconciled, and reporting
        # that as "clean" (or as a divergence) would be a fabricated result.
        try:
            return method(params, timeout=settings.RAZORPAY_TIMEOUT)
        except Exception as exc:
            raise CommandError(
                f"gateway read failed for {what}: {type(exc).__name__}: {exc}"
            ) from exc

    @staticmethod
    def _fetch(method, object_id, what):
        """Fetch one gateway object; ``None`` only when the provider has no such id.

        Razorpay answers a missing payment/refund id with HTTP 400, which the
        SDK maps to ``BadRequestError``. Every other failure is an outage and
        is raised as a CommandError by the caller's contract, never folded
        into "the gateway does not have it".
        """
        try:
            response = method(object_id, timeout=settings.RAZORPAY_TIMEOUT)
        except razorpay.errors.BadRequestError:
            return None
        except Exception as exc:
            raise CommandError(
                f"gateway read failed for {what} {object_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(response, dict):
            raise CommandError(
                f"gateway {what} {object_id} returned no object; refusing to "
                "reconcile an unreadable response"
            )
        if response.get("id") != object_id:
            raise CommandError(
                f"gateway {what} {object_id} answered with id "
                f"{response.get('id')!r}; refusing to reconcile a mismatched object"
            )
        return response

    @staticmethod
    def _emit_alerts(divergences, since, until):
        for key, rows in divergences.items():
            if rows:
                alerts.notify_payment_reconciliation(
                    key, DIVERGENCE_LABELS[key], since, until, rows
                )
