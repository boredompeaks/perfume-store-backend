from django.conf import settings
from django.contrib.admin.models import ADDITION, CHANGE, DELETION
from django.db import transaction
from django.db.models import Q
from django.core.paginator import Paginator
from decimal import Decimal, InvalidOperation

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status

from common.audit import log_api_action
from common.permissions import HasProductsWriteOrReadOnly

from .models import products
from .serializers import ProductSerializer


# Staff-gated writes via permission_classes (conventions.md: never inline
# is_staff). SPEC-6-03c: write authority comes from the ``products.write``
# capability (catalogue + admin roles) — at least as restricted as the
# legacy blanket is_staff gate — while catalogue reads stay public.
@api_view(['GET', 'POST'])
@permission_classes([HasProductsWriteOrReadOnly])
def product_list(request):

    # =========================
    # GET - List Products
    # =========================
    if request.method == 'GET':

        search = request.query_params.get('search')
        category = request.query_params.get('category')
        min_price = request.query_params.get('min_price')
        max_price = request.query_params.get('max_price')
        ordering = request.query_params.get('ordering')

        # Start with all products
        products_data = products.objects.all()

        # Search
        if search:
            products_data = products_data.filter(
                Q(name__icontains=search) |
                Q(description__icontains=search) |
                Q(category__icontains=search)
            )

        # Category filter
        if category:
            products_data = products_data.filter(
                category__iexact=category
            )

        try:
            if min_price is not None:
                min_price = Decimal(min_price)
            if max_price is not None:
                max_price = Decimal(max_price)
        except (InvalidOperation, TypeError):
            return Response(
                {'error': 'Price filters must be valid numbers'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Minimum price
        if min_price:
            products_data = products_data.filter(
                price__gte=min_price
            )

        # Maximum price
        if max_price:
            products_data = products_data.filter(
                price__lte=max_price
            )

        # =========================
        # Ordering
        # =========================

        allowed_ordering = [
            'price',
            '-price',
            'name',
            '-name',
            'created_at',
            '-created_at'
        ]

        if ordering in allowed_ordering:
            products_data = products_data.order_by(ordering)
        else:
            # F-12: an unordered queryset makes Paginator unstable (and
            # emits UnorderedObjectListWarning). When no valid ``ordering``
            # is requested, newest-first is the storefront default, and the
            # unique ``-id`` tiebreaker makes the sort total, so identical
            # requests always partition the catalogue into identical pages.
            products_data = products_data.order_by('-created_at', '-id')

        # =========================
        # Pagination
        # =========================

        page_number = request.query_params.get('page', 1)

        paginator = Paginator(products_data, settings.PRODUCTS_PAGE_SIZE)

        page = paginator.get_page(page_number)

        serializer = ProductSerializer(
            page.object_list,
            many=True
        )

        return Response({
            'count': paginator.count,
            'total_pages': paginator.num_pages,
            'current_page': page.number,
            'next_page': page.has_next(),
            'previous_page': page.has_previous(),
            'results': serializer.data
        })

    # =========================
    # POST - Create Product
    # =========================

    elif request.method == 'POST':

        serializer = ProductSerializer(
            data=request.data
        )

        if serializer.is_valid():
            # [6.12.6] Log privileged actions: the write and its LogEntry
            # commit together, so an audit trail can never lag the row it
            # describes (an unlogged product write is a spec violation).
            with transaction.atomic():
                product = serializer.save()
                log_api_action(request, product, ADDITION, "Created via API.")

            return Response(
                serializer.data,
                status=status.HTTP_201_CREATED
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )


# ==================================
# Single Product Operations
# ==================================

@api_view(['GET', 'PUT', 'PATCH', 'DELETE'])
@permission_classes([HasProductsWriteOrReadOnly])
def product_detail(request, slug):

    # Find product
    try:
        product = products.objects.get(slug=slug)

    except products.DoesNotExist:
        return Response(
            {"error": "Product not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    # =========================
    # GET - Get One Product
    # =========================

    if request.method == 'GET':

        serializer = ProductSerializer(product)

        return Response(
            serializer.data
        )

    # =========================
    # PUT - Full Update
    # =========================

    elif request.method == 'PUT':

        serializer = ProductSerializer(
            product,
            data=request.data
        )

        if serializer.is_valid():
            # [6.12.6] write + audit record commit together (see POST).
            with transaction.atomic():
                product = serializer.save()
                log_api_action(request, product, CHANGE, "Updated via API.")

            return Response(
                serializer.data
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    # =========================
    # PATCH - Partial Update
    # =========================

    elif request.method == 'PATCH':

        serializer = ProductSerializer(
            product,
            data=request.data,
            partial=True
        )

        if serializer.is_valid():
            # [6.12.6] write + audit record commit together (see POST).
            with transaction.atomic():
                product = serializer.save()
                log_api_action(request, product, CHANGE, "Updated via API.")

            return Response(
                serializer.data
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )

    # =========================
    # DELETE - Delete Product
    # =========================

    elif request.method == 'DELETE':

        with transaction.atomic():
            # [6.12.6] Logged before the delete: Django's collector clears
            # the instance pk afterwards, so the record must capture the
            # identity first (the admin's own log_deletion does the same).
            # The record stores the repr and pk (never a FK), so it
            # survives the row it describes.
            log_api_action(request, product, DELETION, "Deleted via API.")
            product.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)
