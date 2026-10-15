"""Unit tests for the pipeline scripts under ``scripts/``.

This package marker exists for one reason: ``unittest discover`` does not
descend into a plain directory, so without it the documented command
(``python -m unittest discover -s scripts -p "test_*.py"``) reports
"NO TESTS RAN" and exits 0. A suite that cannot be discovered is a suite that
cannot fail, which is the exact defect these scripts exist to catch.
"""
