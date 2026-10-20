"""SPEC-D-05: legacy ``Shipment.carrier`` -> ``ShippingProvider`` backfill.

Two writers share one rule: the ``0005`` data migration (historical models) and
the operational command (live models). These pins cover the rule itself, both
writers, and the two properties that make re-running safe: an already-linked
parcel is never overwritten and a second apply finds nothing to do.
"""

import importlib
from decimal import Decimal
from io import StringIO

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from shipping.backfill import matching_provider_ids
from shipping.models import Shipment, ShippingProvider

MIGRATION = importlib.import_module(
    "shipping.migrations.0005_backfill_shipment_provider"
)


def _order(user, suffix):
    from orders.models import Order

    return Order.objects.create(
        user=user,
        full_name="Backfill Buyer",
        phone="9999999999",
        address=f"{suffix} Legacy Lane",
        city="Mumbai",
        state="Maharashtra",
        pincode="400001",
        total_amount=Decimal("100.00"),
    )


class BackfillMappingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="backfill-buyer", password="x")
        self.delhivery = ShippingProvider.objects.create(
            code="delhivery", name="Delhivery"
        )
        # A second provider with the SAME display name is what makes a name
        # match ambiguous; codes stay unique in the schema.
        self.delhivery_twin = ShippingProvider.objects.create(
            code="delhivery-copy", name="Delhivery"
        )
        self.bluedart = ShippingProvider.objects.create(
            code="bluedart", name="Blue Dart"
        )

    def _shipment(self, tracking_number, carrier, provider=None):
        return Shipment.objects.create(
            order=_order(self.user, tracking_number),
            tracking_number=tracking_number,
            carrier=carrier,
            provider=provider,
        )

    def _matches(self, carrier):
        return matching_provider_ids(carrier, list(ShippingProvider.objects.all()))

    def test_code_match_is_case_insensitive(self):
        self.assertEqual(self._matches("DELHIVERY"), {self.delhivery.pk})

    def test_name_match_is_exact_but_not_case_folded(self):
        self.assertEqual(self._matches("Blue Dart"), {self.bluedart.pk})

    def test_two_providers_with_one_name_are_ambiguous(self):
        self.assertEqual(
            self._matches("Delhivery"),
            {self.delhivery.pk, self.delhivery_twin.pk},
        )

    def test_unknown_and_blank_carriers_match_nothing(self):
        self.assertEqual(self._matches("FedEx"), set())
        self.assertEqual(self._matches(""), set())
        self.assertEqual(self._matches(None), set())
        self.assertEqual(self._matches("   "), set())

    def test_already_linked_parcel_is_never_rewritten(self):
        shipment = self._shipment(
            "BF-LINKED", "Delhivery", provider=self.bluedart
        )
        from shipping.backfill import backfill_shipments

        counts = backfill_shipments(apply=True)

        shipment.refresh_from_db()
        self.assertEqual(shipment.provider_id, self.bluedart.pk)
        self.assertEqual(counts["already_linked"], 1)


class BackfillMigrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="migration-buyer", password="x")
        self.delhivery = ShippingProvider.objects.create(
            code="delhivery", name="Delhivery"
        )
        self.bluedart = ShippingProvider.objects.create(
            code="bluedart", name="Blue Dart"
        )

    def _shipment(self, tracking_number, carrier, provider=None):
        return Shipment.objects.create(
            order=_order(self.user, tracking_number),
            tracking_number=tracking_number,
            carrier=carrier,
            provider=provider,
        )

    def test_forward_maps_unambiguous_and_leaves_the_rest_null(self):
        code_match = self._shipment("MG-CODE", "DELHIVERY")
        name_match = self._shipment("MG-NAME", "Blue Dart")
        ambiguous = self._shipment("MG-AMBIGUOUS", "Delhivery")
        # Make "Delhivery" ambiguous for the migration's own pass.
        ShippingProvider.objects.create(code="dh-twin", name="Delhivery")
        unmatched = self._shipment("MG-UNMATCHED", "FedEx")

        MIGRATION.backfill_shipment_providers(django_apps, None)

        code_match.refresh_from_db()
        name_match.refresh_from_db()
        ambiguous.refresh_from_db()
        unmatched.refresh_from_db()
        self.assertEqual(code_match.provider_id, self.delhivery.pk)
        self.assertEqual(name_match.provider_id, self.bluedart.pk)
        self.assertIsNone(ambiguous.provider_id)
        self.assertIsNone(unmatched.provider_id)

    def test_reverse_is_a_documented_noop(self):
        shipment = self._shipment("MG-NOOP", "DELHIVERY")
        MIGRATION.backfill_shipment_providers(django_apps, None)
        shipment.refresh_from_db()
        linked = shipment.provider_id
        self.assertIsNotNone(linked)

        MIGRATION.Migration.operations[0].reverse_code(django_apps, None)

        shipment.refresh_from_db()
        self.assertEqual(shipment.provider_id, linked)


class BackfillCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="command-buyer", password="x")
        self.delhivery = ShippingProvider.objects.create(
            code="delhivery", name="Delhivery"
        )
        self.bluedart = ShippingProvider.objects.create(
            code="bluedart", name="Blue Dart"
        )

    def _shipment(self, tracking_number, carrier):
        return Shipment.objects.create(
            order=_order(self.user, tracking_number),
            tracking_number=tracking_number,
            carrier=carrier,
        )

    def _run(self, *args):
        out = StringIO()
        call_command("backfill_shipment_providers", *args, stdout=out)
        return out.getvalue()

    def test_default_and_dry_run_report_without_writing(self):
        shipment = self._shipment("CMD-1", "DELHIVERY")
        out = self._run()
        self.assertIn("would link 1", out)
        shipment.refresh_from_db()
        self.assertIsNone(shipment.provider_id)

        out = self._run("--dry-run")
        self.assertIn("would link 1", out)
        shipment.refresh_from_db()
        self.assertIsNone(shipment.provider_id)

    def test_apply_links_and_is_idempotent(self):
        code_match = self._shipment("CMD-2", "DELHIVERY")
        name_match = self._shipment("CMD-3", "Blue Dart")
        unmatched = self._shipment("CMD-4", "FedEx")

        out = self._run("--apply")
        self.assertIn("linked 2", out)
        self.assertIn("unmatched 1", out)
        code_match.refresh_from_db()
        name_match.refresh_from_db()
        unmatched.refresh_from_db()
        self.assertEqual(code_match.provider_id, self.delhivery.pk)
        self.assertEqual(name_match.provider_id, self.bluedart.pk)
        self.assertIsNone(unmatched.provider_id)

        out = self._run("--apply")
        self.assertIn("linked 0", out)
        self.assertIn("already linked 2", out)
        code_match.refresh_from_db()
        self.assertEqual(code_match.provider_id, self.delhivery.pk)

    def test_ambiguous_carrier_is_reported_and_left_alone(self):
        ambiguous = self._shipment("CMD-5", "Delhivery")
        ShippingProvider.objects.create(code="dh-twin", name="Delhivery")

        out = self._run("--apply")

        self.assertIn("linked 0", out)
        self.assertIn("ambiguous 1", out)
        ambiguous.refresh_from_db()
        self.assertIsNone(ambiguous.provider_id)
