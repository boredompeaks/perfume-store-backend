from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from .models import Cart, CartItem
from .serializers import CartSerializer
from products.models import products


@api_view(['GET', 'POST'])
def cart_detail(request):

    # Get or create session
    if not request.session.session_key:
        request.session.create()

    session_id = request.session.session_key

    # Get or create cart
    cart, created = Cart.objects.get_or_create(
        session_id=session_id
    )

    # =========================
    # GET - View Cart
    # =========================

    if request.method == 'GET':

        serializer = CartSerializer(cart)

        return Response(
            serializer.data
        )

    # =========================
    # POST - Add Product
    # =========================

    elif request.method == 'POST':

        product_id = request.data.get('product_id')
        quantity = request.data.get('quantity', 1)

        # Check product ID
        if not product_id:
            return Response(
                {"error": "product_id is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Find product
        try:
            product = products.objects.get(
                id=product_id
            )

        except (products.DoesNotExist, ValueError, TypeError):
            # ValueError/TypeError: non-numeric product_id must 404, not 500
            return Response(
                {"error": "Product not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        # Check quantity
        try:
            quantity = int(quantity)

        except (ValueError, TypeError):
            return Response(
                {"error": "Quantity must be a number"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if quantity <= 0:
            return Response(
                {"error": "Quantity must be greater than 0"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check stock
        if quantity > product.stock:
            return Response(
                {"error": "Not enough stock"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Add or update cart item
        cart_item, created = CartItem.objects.get_or_create(
            cart=cart,
            product=product,
            defaults={
                'quantity': quantity
            }
        )

        # If product already exists in cart
        if not created:

            new_quantity = cart_item.quantity + quantity

            if new_quantity > product.stock:
                return Response(
                    {"error": "Not enough stock"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            cart_item.quantity = new_quantity
            cart_item.save()

        # Return updated cart
        serializer = CartSerializer(cart)

        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED
        )
@api_view(['PATCH', 'DELETE'])
def cart_item_detail(request, item_id):

    # =========================
    # Get current session
    # =========================

    if not request.session.session_key:
        return Response(
            {"error": "Cart not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    session_id = request.session.session_key

    # =========================
    # Find cart item
    # =========================

    try:
        cart_item = CartItem.objects.get(
            id=item_id,
            cart__session_id=session_id
        )

    except CartItem.DoesNotExist:
        return Response(
            {"error": "Cart item not found"},
            status=status.HTTP_404_NOT_FOUND
        )

    # =========================
    # PATCH - Update Quantity
    # =========================

    if request.method == 'PATCH':

        quantity = request.data.get('quantity')

        if quantity is None:
            return Response(
                {"error": "quantity is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            quantity = int(quantity)

        except (ValueError, TypeError):
            return Response(
                {"error": "Quantity must be a number"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if quantity <= 0:
            return Response(
                {"error": "Quantity must be greater than 0"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check stock
        if quantity > cart_item.product.stock:
            return Response(
                {"error": "Not enough stock"},
                status=status.HTTP_400_BAD_REQUEST
            )

        cart_item.quantity = quantity
        cart_item.save()

        serializer = CartSerializer(
            cart_item.cart
        )

        return Response(
            serializer.data
        )

    # =========================
    # DELETE - Remove Item
    # =========================

    elif request.method == 'DELETE':

        cart = cart_item.cart

        cart_item.delete()

        serializer = CartSerializer(cart)

        return Response(
            serializer.data,
            status=status.HTTP_200_OK
        )