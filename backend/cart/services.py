"""Cart ownership resolution (D-04a).

Before D-04a the cart was keyed by the browser session alone: a customer who
signed in kept adding to an anonymous cart, checkout resolved that same cart
by session key, and a second device could never see the basket at all. The
rule here is the single place that decides WHICH cart a request owns:

* an authenticated request owns the account cart (one per user, enforced by
  the partial unique constraint on ``Cart.user``);
* an anonymous request owns the cart keyed by its session — the session key
  is still the only credential a guest has.

Every caller resolves through this function, so a user cart and a guest cart
can never be read through each other's key.
"""

from .models import Cart


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
