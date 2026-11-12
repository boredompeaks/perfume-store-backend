"""Cart ownership and the sign-in merge.

This is the single place that decides which cart a request owns: an
authenticated request owns the one account cart, an anonymous one owns the
session-keyed cart; ``merge_session_cart_into_user_cart`` is the only bridge
between the two, run once at sign-in.
"""

from django.db import transaction

from .models import Cart, CartItem


def resolve_cart(request, create=True):
    """The cart this request owns, or None when ``create=False``.

    ``create=False`` is for read/validation paths whose "no cart" is a documented
    404 and must not mint one; the cart endpoint uses the default, whose GET is
    the SPA's boot call and lazily creates the row.
    """
    if request.user.is_authenticated:
        if not create:
            return Cart.objects.filter(user=request.user).first()
        # get_or_create retries the IntegrityError a concurrent first request
        # raises; the partial unique constraint picks the winner.
        cart, _created = Cart.objects.get_or_create(user=request.user)
        return cart

    session_key = request.session.session_key
    if not session_key:
        if not create:
            return None
        # Mint the session so a first-time boot GET has a key to own.
        request.session.create()
        session_key = request.session.session_key

    if not create:
        return Cart.objects.filter(session_id=session_key).first()
    cart, _created = Cart.objects.get_or_create(session_id=session_key)
    return cart


def _merge_items(source, target):
    """Fold ``source``'s lines into ``target``, one line per product.

    Quantities sum, capped at current stock; the cap is advisory (capture
    re-checks the sum), and a line capped to zero is dropped rather than stored.
    """
    for item in source.items.select_related("product"):
        existing = CartItem.objects.filter(cart=target, product=item.product).first()
        wanted = item.quantity + (existing.quantity if existing else 0)
        quantity = min(wanted, item.product.stock)
        if quantity <= 0:
            if existing is not None:
                existing.delete()
            continue
        if existing is None:
            CartItem.objects.create(
                cart=target, product=item.product, quantity=quantity
            )
        else:
            existing.quantity = quantity
            existing.save(update_fields=["quantity"])


def merge_session_cart_into_user_cart(request, user):
    """Move this browser's guest cart into ``user``'s account cart.

    Called immediately after login/registration: the guest cart is adopted in
    place (session key cleared) when the account has none, otherwise its lines
    merge and the target coupon wins. Runs in one transaction with both carts
    locked, so a second tab cannot merge the same rows twice.
    """
    with transaction.atomic():
        source = None
        session_key = request.session.session_key
        if session_key:
            # user__isnull=True: a cart already stamped with an account is not
            # this request's guest basket, whatever its session key says.
            source = (
                Cart.objects.select_for_update()
                .filter(session_id=session_key, user__isnull=True)
                .first()
            )

        if source is None:
            return Cart.objects.filter(user=user).first()

        target, target_created = Cart.objects.select_for_update().get_or_create(
            user=user
        )

        if target_created:
            # ADOPT: the guest row becomes the account cart with its rows intact.
            # The empty row just minted must go first — the partial unique
            # constraint admits exactly one row per user.
            target.delete()
            source.user = user
            source.session_id = None
            source.save(update_fields=["user", "session_id", "updated_at"])
            return source

        _merge_items(source, target)
        if target.coupon_id is None and source.coupon_id is not None:
            # An explicit account coupon is never overwritten by a pre-login one.
            target.coupon = source.coupon
        target.save(update_fields=["coupon", "updated_at"])
        source.delete()
        return target
