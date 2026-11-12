from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.admin.models import ADDITION, CHANGE, DELETION
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from common.audit import (
    log_api_action,
    log_mutation,
    model_field_changes,
)
from common.models import AuditEvent
from common.permissions import HasInventoryAdjust, HasProductsWriteOrReadOnly

from .models import StockMovement, products
from .serializers import ProductSerializer


# Writes are staff/capability-gated via permission_classes (never inline
# is_staff); catalogue reads stay public.
@api_view(["GET", "POST"])
@permission_classes([HasProductsWriteOrReadOnly])
def product_list(request):

    if request.method == "GET":
        search = request.query_params.get("search")
        category = request.query_params.get("category")
        min_price = request.query_params.get("min_price")
        max_price = request.query_params.get("max_price")
        ordering = request.query_params.get("ordering")

        # Base queryset is the on-sale catalogue, so no downstream filter or
        # search can reintroduce a delisted row; staff surfaces keep every row.
        products_data = products.objects.filter(is_active=True)

        if search:
            products_data = products_data.filter(
                Q(name__icontains=search)
                | Q(description__icontains=search)
                | Q(category__icontains=search)
            )

        if category:
            products_data = products_data.filter(category__iexact=category)

        try:
            if min_price is not None:
                min_price = Decimal(min_price)
            if max_price is not None:
                max_price = Decimal(max_price)
        except (InvalidOperation, TypeError):
            return Response(
                {"error": "Price filters must be valid numbers"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Tested against None, not truthiness: "0" is a valid floor and is falsy.
        if min_price is not None:
            products_data = products_data.filter(price__gte=min_price)

        if max_price is not None:
            products_data = products_data.filter(price__lte=max_price)

        allowed_ordering = [
            "price",
            "-price",
            "name",
            "-name",
            "created_at",
            "-created_at",
        ]

        if ordering in allowed_ordering:
            products_data = products_data.order_by(ordering)
        else:
            # Paginator needs ordering: newest-first, with ``-id`` as the
            # tiebreaker so identical requests partition identically.
            products_data = products_data.order_by("-created_at", "-id")

        page_number = request.query_params.get("page", 1)

        paginator = Paginator(products_data, settings.PRODUCTS_PAGE_SIZE)

        page = paginator.get_page(page_number)

        serializer = ProductSerializer(page.object_list, many=True)

        return Response(
            {
                "count": paginator.count,
                "total_pages": paginator.num_pages,
                "current_page": page.number,
                "next_page": page.has_next(),
                "previous_page": page.has_previous(),
                "results": serializer.data,
            }
        )

    elif request.method == "POST":
        serializer = ProductSerializer(data=request.data)

        if serializer.is_valid():
            # [6.12.6] Write and LogEntry commit together, so the audit trail
            # can never lag the row it describes.
            with transaction.atomic():
                product = serializer.save()
                log_api_action(request, product, ADDITION, "Created via API.")
                # Structured mutation event: real before (None) -> after values
                # plus the surface the write arrived through.
                log_mutation(
                    request,
                    product,
                    AuditEvent.EventType.CATALOGUE_CREATED,
                    "created",
                    AuditEvent.Source.API,
                    changes=model_field_changes(product, serializer.validated_data),
                )

            return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


def _save_product_update(request, serializer):
    """Apply a validated PUT/PATCH and record what actually changed.

    The pre-write snapshot is taken before ``save()`` because the old values
    are gone after it; both the LogEntry and the mutation event are written
    from this one place so PUT and PATCH cannot drift apart.
    """
    product = serializer.instance
    before = {name: getattr(product, name, None) for name in serializer.validated_data}
    product = serializer.save()
    log_api_action(request, product, CHANGE, "Updated via API.")
    log_mutation(
        request,
        product,
        AuditEvent.EventType.CATALOGUE_UPDATED,
        "updated",
        AuditEvent.Source.API,
        changes=model_field_changes(product, serializer.validated_data, before=before),
    )
    return product


@api_view(["GET", "PUT", "PATCH", "DELETE"])
@permission_classes([HasProductsWriteOrReadOnly])
def product_detail(request, slug):

    try:
        product = products.objects.get(slug=slug)

    except products.DoesNotExist:
        return Response(
            {"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND
        )

    if request.method == "GET":
        # Delisted rows 404 byte-identically here, so no existence oracle;
        # writes still address them (gated) for delist -> edit -> relist.
        if not product.is_active:
            return Response(
                {"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND
            )

        serializer = ProductSerializer(product)

        return Response(serializer.data)

    elif request.method == "PUT":
        serializer = ProductSerializer(product, data=request.data)

        if serializer.is_valid():
            # [6.12.6] write + audit record commit together (see POST).
            with transaction.atomic():
                product = _save_product_update(request, serializer)

            return Response(serializer.data)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    elif request.method == "PATCH":
        serializer = ProductSerializer(product, data=request.data, partial=True)

        if serializer.is_valid():
            # [6.12.6] write + audit record commit together (see POST).
            with transaction.atomic():
                product = _save_product_update(request, serializer)

            return Response(serializer.data)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    elif request.method == "DELETE":
        with transaction.atomic():
            # Logged before the delete: Django's collector clears the pk, and
            # the record stores repr + pk so it survives the row it names.
            log_api_action(request, product, DELETION, "Deleted via API.")
            # A deletion has no "after", but still records its surface and
            # target on a row that outlives the object.
            log_mutation(
                request,
                product,
                AuditEvent.EventType.CATALOGUE_DELETED,
                "deleted",
                AuditEvent.Source.API,
            )
            product.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


# Out-of-set reasons must be a 400 here, never a 500 at insert time.
_VALID_ADJUSTMENT_REASONS = frozenset(StockMovement.Reason.values)


def _object_body(data):
    """The write body as a dict, or ``{}`` when it is not a JSON object.

    A JSON array/scalar/null parses but has no ``.get``, which raised a 500;
    the empty object routes it into the normal missing-key 400. Duplicated
    from ``cart.views`` rather than importing that whole view stack for a
    one-liner.
    """
    return data if isinstance(data, dict) else {}


@api_view(["POST"])
@permission_classes([HasInventoryAdjust])
def inventory_adjust(request):
    """POST /admin/inventory/adjustments: thin REST wrapper over ``adjust_stock``.

    The service owns the contract (atomic + select_for_update, below-zero
    guard, ledger row); this only maps HTTP onto it, and the movement row is
    the audit record.
    """
    body = _object_body(request.data)

    product_id = body.get("product_id")
    delta = body.get("delta")
    reason = body.get("reason")
    note = body.get("note", "")

    if product_id is None:
        return Response(
            {"error": "product_id is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    if delta is None:
        return Response(
            {"error": "delta is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    # bool is an int subclass: True/False must not masquerade as ±1, and a
    # float/str delta would silently truncate or slip past the service.
    if not isinstance(delta, int) or isinstance(delta, bool):
        return Response(
            {"error": "delta must be an integer"}, status=status.HTTP_400_BAD_REQUEST
        )

    if reason is None:
        return Response(
            {"error": "reason is required"}, status=status.HTTP_400_BAD_REQUEST
        )

    if reason not in _VALID_ADJUSTMENT_REASONS:
        return Response(
            {
                "error": "reason must be one of: "
                + ", ".join(StockMovement.Reason.values)
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        product = products.objects.get(id=product_id)
    except products.DoesNotExist:
        return Response(
            {"error": "Product not found"}, status=status.HTTP_404_NOT_FOUND
        )

    try:
        product.adjust_stock(request.user, delta, reason, note)
    except ValueError as error:
        # Below-zero guard rejects before any write, so the message is safe
        # to surface verbatim.
        return Response({"error": str(error)}, status=status.HTTP_400_BAD_REQUEST)

    movement = product.stock_movements.first()

    return Response(
        {
            "movement_id": movement.id,
            "product_id": product.id,
            "delta": movement.delta,
            "stock_after": movement.stock_after,
            "reason": movement.reason,
            "note": movement.note,
        },
        status=status.HTTP_201_CREATED,
    )
