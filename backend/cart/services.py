"""Cart ownership (D-04a) and the sign-in merge (D-04b).

Before D-04a the cart was keyed by the browser session alone: a customer who
signed in kept adding to an anonymous cart, checkout resolved that same cart
by session key, and a second device could never see the basket at all. The
rule here is the single place that decides WHICH cart a request owns:

* an authenticated request owns the account cart (one per user, enforced by
  the partial unique constraint on ``Cart.user``);
* an anonymous request owns the cart keyed by its session — the session key
  is still the only credential a guest has.

Every caller resolves through ``resolve_cart``, so a user cart and a guest
cart can never be read through each other's key. ``merge_session_cart_into_
user_cart`` is the one bridge between the two, run once at sign-in.
"""

from django.db import transaction

from .models import Cart, CartItem


def resolve_cart(request, create=True):
    """The cart this request owns, or ``None`` when ``create=False``.

    ``create=False`` is for the read/validation paths (checkout, coupon
    apply) whose "no cart" answer is a documented 404: they must not mint an
    empty cart as a side effect of being asked. The cart endpoint itself uses
    the default, because its GET is the SPA's boot call and creating the row
    then is the documented lazy-creation contract.
    """
    if request.user.is_authenticated:
        if not create:
            return Cart.objects.filter(user=request.user).first()
        # get_or_create is the IntegrityError-retrying shape conventions.md
        # asks for: a concurrent first request from two tabs converges on the
        # row the partial unique constraint admits instead of raising.
        cart, _created = Cart.objects.get_or_create(user=request.user)
        return cart

    session_key = request.session.session_key
    if not session_key:
        if not create:
            return None
        # Mint the session so a first-time boot GET finds (or lazily
        # creates) its cart instead of failing.
        request.session.create()
        session_key = request.session.session_key

    if not create:
        return Cart.objects.filter(session_id=session_key).first()
    cart, _created = Cart.objects.get_or_create(session_id=session_key)
    return cart


def _merge_items(source, target):
    """Fold ``source``'s lines into ``target``, one line per product.

    Quantities SUM, capped at the product's current stock. The cap is the
    same advisory gate the add-to-cart endpoint applies: stock can still drop
    before payment, so the authoritative check remains the summed re-check in
    the capture path — this only keeps an impossible line out of the row both
    sides can see. A line whose cap is zero carries no purchasable quantity
    and is dropped rather than stored as a zero-quantity row.
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

    Called immediately after a successful login or registration, while the
    request still carries the session cookie the guest basket was built
    under. Returns the cart the account now owns, or ``None`` when there was
    no guest basket and the account had no cart either — a sign-in with
    nothing to merge must not mint an empty cart, which would turn checkout's
    documented "Cart not found" into a different answer. The next cart GET
    creates the row lazily anyway.

    * Account cart absent: the guest cart is ADOPTED rather than copied —
      stamped with the user and stripped of its session key, so the cookie
      can never resolve back into the account after logout.
    * Otherwise the lines merge (summed, stock-capped, one per product), the
      target coupon wins, and the emptied guest cart is deleted.

    The whole flow runs in one transaction with both carts locked: a second
    tab signing in at the same time must not merge the same rows twice, and
    an adopted cart must never remain addressable by its old session key.
    """
    with transaction.atomic():
        source = None
        session_key = request.session.session_key
        if session_key:
            # user__isnull=True: a cart already stamped with an account is
            # not this request's guest basket, whatever its session key says.
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
            # ADOPT: the guest row becomes the account cart with its rows
            # intact. The empty row get_or_create just minted is discarded
            # first — the partial unique constraint admits exactly one row
            # per user, so adopting in place requires the slot to be free.
            target.delete()
            source.user = user
            source.session_id = None
            source.save(update_fields=["user", "session_id", "updated_at"])
            return source

        _merge_items(source, target)
        if target.coupon_id is None and source.coupon_id is not None:
            # The coupon the guest applied follows the basket unless the
            # account cart already holds one — an explicit account choice is
            # never silently overwritten by a pre-login one.
            target.coupon = source.coupon
        target.save(update_fields=["coupon", "updated_at"])
        source.delete()
        return target
