from django.test import SimpleTestCase, tag

from orders import state as order_state


@tag("orders")
class PaymentMethodWireLiteralTests(SimpleTestCase):
    def test_payment_method_wire_literals(self):
        # Hand-written, never order_state.PAYMENT_METHOD_*: migration 0014 hardcodes these values
        # in the model choices, so the constant is the only thing that may move.
        self.assertEqual(order_state.PAYMENT_METHOD_COD, "cod")
        self.assertEqual(order_state.PAYMENT_METHOD_PREPAID, "prepaid")
        self.assertEqual(
            [value for value, _label in order_state.PAYMENT_METHOD_CHOICES],
            ["prepaid", "cod"],
        )


@tag("orders")
class StatusEventTriggerWireLiteralTests(SimpleTestCase):
    def test_trigger_wire_literals(self):
        # Hand-written, never order_state.TRIGGER_*: a recomputed oracle agrees with a wrong constant
        # from both sides.
        self.assertEqual(order_state.TRIGGER_ORDER_CREATE, "order_create")
        self.assertEqual(order_state.TRIGGER_ADMIN_CHANGE_FORM, "admin_change_form")
        self.assertEqual(order_state.TRIGGER_ADMIN_BULK_ACTION, "admin_bulk_action")
        self.assertEqual(order_state.TRIGGER_PAYMENT_VERIFY, "payment_verify")
        self.assertEqual(order_state.TRIGGER_ADMIN_API_FULFIL, "admin_api_fulfil")
        self.assertEqual(order_state.TRIGGER_ADMIN_API_CANCEL, "admin_api_cancel")
        self.assertEqual(order_state.TRIGGER_PAYMENT_FAILED, "payment_failed")
        self.assertEqual(order_state.TRIGGER_PAYMENT_WEBHOOK, "payment_webhook")

    def test_registered_triggers_are_exactly_the_eight_migration_spellings(self):
        # Values are the migration-recorded contract; labels are display text and deliberately
        # not pinned. Order matters: it is the admin widget order.
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
