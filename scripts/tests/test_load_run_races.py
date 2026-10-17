"""Unit tests for ``scripts/load/run_races.py``'s invariant-check pipeline.

Auditor BUG-1 (cycle 1): the T2-INV-6 expectation shipped in a ``value_from``
form that ``checks_for_phase`` never resolved and ``evaluate_checks`` did not
understand, so EVERY double-capture run reported INV-6 FAIL on a perfectly
clean DB — in both live-psycopg and --verify-json mode — and the
exactly-once-capture invariant (the core deliverable) was unverifiable.

These tests pin both directions of the check with the auditor's exact rows,
through BOTH consumption shapes the runner uses:

  * the resolved ``row_eq/value`` form (what checks_for_phase emits and what
    an --verify-json round-trip file carries), and
  * the raw ``row_eq/value_from`` form plus a context dict (the belt the
    evaluator keeps for a hand-written/stale checks file).

Everything here is offline: no HTTP, no DB, no network — the module's HTTP
plumbing is never invoked, only its pure functions.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "load"))

import run_races as rr  # noqa: E402

# The phase results dict phase_double_capture produces (run_races.py), with
# the auditor's named ids: order 77 captured once under payment id payX.
RESULTS = {
    "capture_order_id": 77,
    "capture_payment_id": "payX",
    "razorpay_order_id": "order_ABC",
    "expected_captured": 1,
}

# The auditor's exact query rows: a clean capture (one transition) and a
# genuine double-capture shape (two pending->confirmed transitions).
CLEAN_ROWS = [
    {
        "payment_status": "captured",
        "razorpay_payment_id": "payX",
        "capture_transitions": 1,
    }
]
DOUBLE_CAPTURED_ROWS = [
    {
        "payment_status": "captured",
        "razorpay_payment_id": "payX",
        "capture_transitions": 2,
    }
]

INV6_ID = "T2-INV-6-capture-moved-once"


def _resolved_check():
    checks = rr.checks_for_phase("double-capture", dict(RESULTS))
    return next(c for c in checks if c["id"] == INV6_ID)


class ValueFromResolutionTests(unittest.TestCase):
    def test_context_keys_resolve_and_literals_pass_through(self):
        resolved = rr.resolve_value_from(
            {
                "payment_status": "captured",
                "razorpay_payment_id": "capture_payment_id",
                "capture_transitions": 1,
            },
            RESULTS,
        )
        self.assertEqual(
            resolved,
            {
                "payment_status": "captured",
                "razorpay_payment_id": "payX",
                "capture_transitions": 1,
            },
        )

    def test_missing_context_key_stays_a_visible_literal_not_a_crash(self):
        resolved = rr.resolve_value_from(
            {"razorpay_payment_id": "capture_payment_id"}, {}
        )
        self.assertEqual(resolved, {"razorpay_payment_id": "capture_payment_id"})

    def test_checks_for_phase_resolves_inv6_to_a_concrete_value_form(self):
        check = _resolved_check()
        self.assertEqual(check["expect"]["rule"], "row_eq")
        self.assertNotIn("value_from", check["expect"])
        self.assertEqual(
            check["expect"]["value"],
            {
                "payment_status": "captured",
                "razorpay_payment_id": "payX",
                "capture_transitions": 1,
            },
        )


class Inv6EvaluationTests(unittest.TestCase):
    def test_clean_capture_passes_in_resolved_form(self):
        # verify-json / live shape: the check carries row_eq/value already.
        verdicts = rr.evaluate_checks([_resolved_check()], {INV6_ID: CLEAN_ROWS})
        self.assertEqual(len(verdicts), 1)
        self.assertTrue(verdicts[0]["ok"], verdicts[0])

    def test_clean_capture_passes_in_raw_value_from_form_with_context(self):
        # live-mode belt: a checks file that still names context keys must
        # evaluate, not report "unsupported expect form".
        raw = {
            "id": INV6_ID,
            "expect": {
                "rule": "row_eq",
                "value_from": {
                    "payment_status": "captured",
                    "razorpay_payment_id": "capture_payment_id",
                    "capture_transitions": 1,
                },
            },
        }
        verdicts = rr.evaluate_checks(
            [raw], {INV6_ID: CLEAN_ROWS}, context=dict(RESULTS)
        )
        self.assertTrue(verdicts[0]["ok"], verdicts[0])

    def test_genuine_double_capture_shape_fails(self):
        verdicts = rr.evaluate_checks(
            [_resolved_check()], {INV6_ID: DOUBLE_CAPTURED_ROWS}
        )
        self.assertFalse(verdicts[0]["ok"])
        self.assertIn("capture_transitions", verdicts[0]["detail"])

    def test_wrong_payment_reference_fails(self):
        rows = [dict(CLEAN_ROWS[0], razorpay_payment_id="payOTHER")]
        verdicts = rr.evaluate_checks([_resolved_check()], {INV6_ID: rows})
        self.assertFalse(verdicts[0]["ok"])
        self.assertIn("razorpay_payment_id", verdicts[0]["detail"])


class IntentCapAlignmentTests(unittest.TestCase):
    def test_intent_cap_default_is_twenty(self):
        # Auditor BUG-4: the two files' LOAD_MAX_INTENTS fallbacks diverged
        # (20 in k6_profiles.js, 25 here). The runner's default is pinned to
        # 20 by hand-written literal, NOT derived from anything.
        self.assertEqual(rr.INTENT_CAP, 20)


if __name__ == "__main__":
    unittest.main()
