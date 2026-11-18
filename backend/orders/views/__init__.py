"""The orders view layer, split by domain. Every name the former single-file
``orders/views.py`` published is re-exported here, so ``from orders.views import
X`` keeps working for the urlconf, ``cart.views`` and the test suite.
"""

# ``return`` is a Python keyword, so the module is bound through importlib;
# every name below is the object a normal ``from .return import`` would give.
from importlib import import_module as _import_module

# Module objects the suite patches through this package's own attribute path.
import razorpay
from django.utils import timezone

from common import notifications

# Identity-pinned against orders.state by the suite.
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
    GUEST_TOKEN_HEADER,
    GUEST_TOKEN_MAX_LENGTH,
    IDEMPOTENCY_KEY_HEADER,
    IDEMPOTENCY_KEY_MAX_LENGTH,
    ORDER_NUMBER_ATTEMPTS,
    SHIPPING_METHOD_FIELD,
    _checkout_response,
    _current_year,
    _field_length_rejection,
    _find_duplicate_pending_order,
    _generate_order_number,
    _guest_checkout_refused,
    _guest_lookup_miss,
    _mint_order_reservations,
    _order_lines,
    _requested_shipping_code,
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
from .refund import _refund_payload, admin_order_refund, refund_request

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
    "timezone",
    "_may_fulfil",
    "admin_order_cancel",
    "admin_order_detail",
    "admin_order_fulfill",
    "admin_order_list",
    "GUEST_TOKEN_HEADER",
    "GUEST_TOKEN_MAX_LENGTH",
    "IDEMPOTENCY_KEY_HEADER",
    "IDEMPOTENCY_KEY_MAX_LENGTH",
    "ORDER_NUMBER_ATTEMPTS",
    "SHIPPING_METHOD_FIELD",
    "_SHIPPING_FIELDS",
    "_checkout_response",
    "_current_year",
    "_field_length_rejection",
    "_find_duplicate_pending_order",
    "_generate_order_number",
    "_guest_checkout_refused",
    "_guest_lookup_miss",
    "_mint_order_reservations",
    "_order_lines",
    "_requested_shipping_code",
    "_same_purchase",
    "_same_shipping_choice",
    "_uniform_coupon_rejection",
    "apply_coupon",
    "create_order",
    "guest_order_detail",
    "validate_redeemable_coupon",
    "HISTORY_PAGE_SIZE_QUERY_PARAM",
    "_history_page_size",
    "order_detail",
    "order_list",
    "PAYMENT_INTENT_ATTEMPTS",
    "create_payment",
    "verify_payment",
    "_refund_payload",
    "admin_order_refund",
    "refund_request",
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
