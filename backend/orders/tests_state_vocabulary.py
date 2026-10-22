"""[MUT-2b] Hand-written literal pins for the state-machine wire vocabulary.

The 2026-10-12 mutation run survived every value-mutant on these constants
because every other test derives its expected value from the constant under
test — a recomputed oracle agrees with a wrong constant from both sides —
while the spellings are contracts recorded elsewhere: migration 0014
hardcodes ``cod`` in the payment-method choices and migration 0016 hardcodes
all eight audit-trigger values in the OrderStatusEvent trigger choices. A
rename in this module would silently drift the Python vocabulary from its
migration-recorded spellings with the whole suite green.

These pins are hand-written literals, never derived from the constants, so
a renamed value fails here. They are deliberately SimpleTestCase-cheap: the
contract is the literal, not any behavior that could re-derive it.
"""

from django.test import SimpleTestCase, tag

from orders import state as order_state


@tag("orders")
class PaymentMethodWireLiteralTests(SimpleTestCase):
    """The COD/prepaid wire spellings, pinned against migration 0014."""

    def test_payment_method_wire_literals(self):
        # Hand-written, never order_state.PAYMENT_METHOD_*: migration 0014
        # hardcodes these values in the Order.payment_method choices, so the
        # constant is the only thing that may move — and it may not.
        self.assertEqual(order_state.PAYMENT_METHOD_COD, "cod")
        self.assertEqual(order_state.PAYMENT_METHOD_PREPAID, "prepaid")
        self.assertEqual(
            [value for value, _label in order_state.PAYMENT_METHOD_CHOICES],
            ["prepaid", "cod"],
        )


@tag("orders")
class StatusEventTriggerWireLiteralTests(SimpleTestCase):
    """The audit-trigger wire spellings, pinned against migration 0016.

    Migration 0016 hardcodes all eight values in the OrderStatusEvent
    trigger choices; the weekly mutation run renamed every constant in turn
    and the suite stayed green because its trigger assertions recompute the
    expected value from the constant being mutated.
    """

    def test_trigger_wire_literals(self):
        # Hand-written, never order_state.TRIGGER_*: the recomputed-oracle
        # pattern is exactly what let the 2026-10-12 run survive these.
        self.assertEqual(order_state.TRIGGER_ORDER_CREATE, "order_create")
        self.assertEqual(order_state.TRIGGER_ADMIN_CHANGE_FORM, "admin_change_form")
        self.assertEqual(order_state.TRIGGER_ADMIN_BULK_ACTION, "admin_bulk_action")
        self.assertEqual(order_state.TRIGGER_PAYMENT_VERIFY, "payment_verify")
        self.assertEqual(order_state.TRIGGER_ADMIN_API_FULFIL, "admin_api_fulfil")
        self.assertEqual(order_state.TRIGGER_ADMIN_API_CANCEL, "admin_api_cancel")
        self.assertEqual(order_state.TRIGGER_PAYMENT_FAILED, "payment_failed")
        self.assertEqual(order_state.TRIGGER_PAYMENT_WEBHOOK, "payment_webhook")

    def test_registered_triggers_are_exactly_the_eight_migration_spellings(self):
        # The registration list is the vocabulary a writer may pick from;
        # its VALUES are the migration-recorded contract, its labels are
        # display text and deliberately not pinned here (WEAK per the
        # mutation triage). Order matters: it is the admin widget order.
        self.assertEqual(
            [value for value, _label in order_state.STATUS_EVENT_TRIGGERS],
            [
                "order_create",
                "admin_change_form",
                "admin_bulk_action",
                "payment_verify",
                "admin_api_fulfil",
                "admin_api_cancel",
                "payment_failed",
                "payment_webhook",
            ],
        )
