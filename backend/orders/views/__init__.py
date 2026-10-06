"""The orders view layer, split by domain.

This package is the SAME public surface the former single-file `orders/views.py`
exposed: every name that module bound at module level is re-exported here, so
`from orders.views import X` keeps working for the urlconf, for `cart.views`,
and for the test suite unchanged. The bodies themselves were not rewritten -
they moved verbatim into the six domain modules below.

WHAT MOVED WHERE:

* `checkout` - order creation, the two collapse guards, the guest credential
  invariant, the guest order read, and the coupon preview.
* `payment`  - the gateway intent and the verify that confirms money.
* `refund`   - the staff refund seam.
* `return`   - the customer's returns family (create, list, detail).
* `order`    - the customer's own order history and detail.
* `admin`    - the staff order seam (unscoped list/detail, fulfil, cancel).

The cross-module edges are deliberately few and one-way: `admin` borrows the
page-size contract from `order`, `refund` borrows the idempotency-key header
names from `checkout`, and `return` borrows the shared query-param name from
`order`. Nothing imports back, so there is no cycle to reason about.

`__all__` is declared rather than left implicit, and it is the reason this file
needs no per-name "re-exported" noqa: the names below are this package's
published surface, not unused imports. Three groups are in it that are not view
code, each for a stated reason:

* the four module objects (`razorpay`, `notifications`, `secrets`, `timezone`)
  that the test suite patches THROUGH this package - e.g. the Razorpay client
  faked at `orders.views.razorpay.Client`, and the clock frozen at
  `orders.views.timezone.now`. Patching an attribute on the real module object
  reaches every importer, so re-exporting the object keeps those patch targets
  resolving without any of them knowing about the split.
* three names from `orders.state`, so the suite's identity pins
  (`orders_views.ALLOWED_TRANSITIONS is order_state.ALLOWED_TRANSITIONS` and
  its two siblings) compare the one object rather than a copy.
* the private helpers, which the suite imports directly.

ONE NAME FROM THE OLD MODULE IS DELIBERATELY ABSENT: `logger`. The old file
bound one module-level logger; after the split the records are emitted by six
module-level loggers, one per domain module, named for the module that emitted
them. Re-exporting any single one here would publish a logger that never logs,
which is worse than its absence - a reader would reasonably assume records
arrived under this name. Nothing imports or patches `orders.views.logger`
(checked across the tree), and `assertLogs("orders.views")` still captures every
domain record because a child logger propagates to its ancestors' handlers.
"""

# Module objects the suite patches through this package's own attribute path.
import secrets

# `return` is a Python KEYWORD, so `from .return import x` is a SyntaxError and
# that module is unreachable by ordinary import syntax however it is named. The
# file keeps the ratified name `return.py`; it is simply bound through importlib
# instead. Every name bound from it below is the one a normal
# `from .return import` would have produced - same objects, same identity - and a
# patch target that spells the path as a string
# ("orders.views.return._return_window_days") still resolves, because mock and
# importlib both reach the module by name.
from importlib import import_module as _import_module

import razorpay
from django.utils import timezone

from common import notifications

# Identity-pinned against orders.state by the suite; re-exported so the pin
# compares the one object.
from ..state import ADMIN_FULFILMENT_NEXT, ALLOWED_TRANSITIONS, transition_allowed
from .admin import (
    _may_fulfil,
    admin_order_cancel,
    admin_order_detail,
    admin_order_fulfill,
    admin_order_list,
)
from .checkout import (
    _SHIPPING_FIELDS,
    GUEST_TOKEN_BYTES,
    GUEST_TOKEN_HEADER,
    GUEST_TOKEN_MAX_LENGTH,
    IDEMPOTENCY_KEY_HEADER,
    IDEMPOTENCY_KEY_MAX_LENGTH,
    ORDER_NUMBER_ATTEMPTS,
    SHIPPING_METHOD_FIELD,
    _canonical_guest_email,
    _checkout_owner,
    _checkout_response,
    _current_year,
    _field_length_rejection,
    _find_duplicate_pending_order,
    _generate_order_number,
    _guest_lookup_miss,
    _idempotency_key_conflict,
    _mint_guest_token,
    _mint_order_reservations,
    _next_order_sequence,
    _order_lines,
    _requested_shipping_code,
    _resolve_checkout_owner,
    _same_purchase,
    _same_shipping_choice,
    _uniform_coupon_rejection,
    apply_coupon,
    create_order,
    guest_order_detail,
    validate_redeemable_coupon,
)
from .order import (
    HISTORY_PAGE_SIZE_QUERY_PARAM,
    _history_page_size,
    order_detail,
    order_list,
)
from .payment import PAYMENT_INTENT_ATTEMPTS, create_payment, verify_payment
from .refund import _refund_payload, admin_order_refund

_return = _import_module(f"{__name__}.return")

RETURN_REFUSAL_BODY_KEY = _return.RETURN_REFUSAL_BODY_KEY
RETURN_REFUSAL_ERRORS = _return.RETURN_REFUSAL_ERRORS
RETURN_REFUSAL_NOT_ELIGIBLE = _return.RETURN_REFUSAL_NOT_ELIGIBLE
RETURN_REFUSAL_OUTSIDE_WINDOW = _return.RETURN_REFUSAL_OUTSIDE_WINDOW
RETURN_REFUSAL_WINDOW_CLOSED = _return.RETURN_REFUSAL_WINDOW_CLOSED
RETURNS_PAGE_SIZE_QUERY_PARAM = _return.RETURNS_PAGE_SIZE_QUERY_PARAM
MalformedReturnRequestBody = _return.MalformedReturnRequestBody
_RETURN_BODY_FIELDS = _return._RETURN_BODY_FIELDS
_create_return_request = _return._create_return_request
_list_return_requests = _return._list_return_requests
_return_body = _return._return_body
_return_detail_miss = _return._return_detail_miss
_return_eligible = _return._return_eligible
_return_request_miss = _return._return_request_miss
_return_request_payload = _return._return_request_payload
_return_window_anchor = _return._return_window_anchor
_return_window_days = _return._return_window_days
_return_window_refusal = _return._return_window_refusal
_returns_closed = _return._returns_closed
_returns_page_size = _return._returns_page_size
return_request_detail = _return.return_request_detail
return_requests = _return.return_requests

__all__ = [
    # orders.state, re-exported for the suite's identity pins
    "ADMIN_FULFILMENT_NEXT",
    "ALLOWED_TRANSITIONS",
    "transition_allowed",
    # module objects the suite patches through this package
    "notifications",
    "razorpay",
    "secrets",
    "timezone",
    # admin
    "_may_fulfil",
    "admin_order_cancel",
    "admin_order_detail",
    "admin_order_fulfill",
    "admin_order_list",
    # checkout
    "GUEST_TOKEN_BYTES",
    "GUEST_TOKEN_HEADER",
    "GUEST_TOKEN_MAX_LENGTH",
    "IDEMPOTENCY_KEY_HEADER",
    "IDEMPOTENCY_KEY_MAX_LENGTH",
    "ORDER_NUMBER_ATTEMPTS",
    "SHIPPING_METHOD_FIELD",
    "_SHIPPING_FIELDS",
    "_canonical_guest_email",
    "_checkout_owner",
    "_checkout_response",
    "_current_year",
    "_field_length_rejection",
    "_find_duplicate_pending_order",
    "_generate_order_number",
    "_guest_lookup_miss",
    "_idempotency_key_conflict",
    "_mint_guest_token",
    "_mint_order_reservations",
    "_next_order_sequence",
    "_order_lines",
    "_requested_shipping_code",
    "_resolve_checkout_owner",
    "_same_purchase",
    "_same_shipping_choice",
    "_uniform_coupon_rejection",
    "apply_coupon",
    "create_order",
    "guest_order_detail",
    "validate_redeemable_coupon",
    # order
    "HISTORY_PAGE_SIZE_QUERY_PARAM",
    "_history_page_size",
    "order_detail",
    "order_list",
    # payment
    "PAYMENT_INTENT_ATTEMPTS",
    "create_payment",
    "verify_payment",
    # refund
    "_refund_payload",
    "admin_order_refund",
    # return
    "RETURN_REFUSAL_BODY_KEY",
    "RETURN_REFUSAL_ERRORS",
    "RETURN_REFUSAL_NOT_ELIGIBLE",
    "RETURN_REFUSAL_OUTSIDE_WINDOW",
    "RETURN_REFUSAL_WINDOW_CLOSED",
    "RETURNS_PAGE_SIZE_QUERY_PARAM",
    "MalformedReturnRequestBody",
    "_RETURN_BODY_FIELDS",
    "_create_return_request",
    "_list_return_requests",
    "_return_body",
    "_return_detail_miss",
    "_return_eligible",
    "_return_request_miss",
    "_return_request_payload",
    "_return_window_anchor",
    "_return_window_days",
    "_return_window_refusal",
    "_returns_closed",
    "_returns_page_size",
    "return_request_detail",
    "return_requests",
]
