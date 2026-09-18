"""Staff role definitions for RBAC (spec 6.12).

This module is the single source of truth for staff roles and their
capabilities. Later micro-tasks (permissions.py, staff admin endpoints)
resolve authorization checks against ``CAPABILITY_ROLES`` instead of
re-hardcoding role names, and Django Groups are synced from ``STAFF_ROLES``
so group membership can back the role assignment.
"""

from django.contrib.auth.models import Group

ROLE_SUPPORT = "support"
ROLE_CATALOGUE = "catalogue"
ROLE_INVENTORY = "inventory"
ROLE_MARKETING = "marketing"
ROLE_FINANCE = "finance"
ROLE_ADMIN = "admin"

# Stable, ordered tuple of the six staff roles (spec 6.12).
STAFF_ROLES = (
    ROLE_SUPPORT,
    ROLE_CATALOGUE,
    ROLE_INVENTORY,
    ROLE_MARKETING,
    ROLE_FINANCE,
    ROLE_ADMIN,
)

# Capability identifier (spec 6.12 examples) -> roles granted that capability.
# Least privilege by default: each role holds only what its function needs,
# and only ``admin`` holds the sensitive staff/settings capabilities so that
# role changes themselves stay an admin-only concern.
CAPABILITY_ROLES = {
    "products.read": frozenset(
        {ROLE_SUPPORT, ROLE_CATALOGUE, ROLE_INVENTORY, ROLE_MARKETING, ROLE_ADMIN}
    ),
    "products.write": frozenset({ROLE_CATALOGUE, ROLE_ADMIN}),
    "products.publish": frozenset({ROLE_CATALOGUE, ROLE_ADMIN}),
    "inventory.read": frozenset({ROLE_INVENTORY, ROLE_CATALOGUE, ROLE_ADMIN}),
    "inventory.adjust": frozenset({ROLE_INVENTORY, ROLE_ADMIN}),
    "orders.read": frozenset({ROLE_SUPPORT, ROLE_FINANCE, ROLE_ADMIN}),
    "orders.fulfill": frozenset({ROLE_SUPPORT, ROLE_ADMIN}),
    "orders.cancel": frozenset({ROLE_SUPPORT, ROLE_ADMIN}),
    "refunds.create": frozenset({ROLE_FINANCE, ROLE_ADMIN}),
    "customers.read": frozenset({ROLE_SUPPORT, ROLE_FINANCE, ROLE_ADMIN}),
    "discounts.write": frozenset({ROLE_MARKETING, ROLE_ADMIN}),
    "reports.read": frozenset({ROLE_FINANCE, ROLE_MARKETING, ROLE_ADMIN}),
    "staff.manage": frozenset({ROLE_ADMIN}),
    "settings.manage": frozenset({ROLE_ADMIN}),
}


def sync_role_groups():
    """Create the six staff role groups, returning them keyed by role name.

    Idempotent via ``get_or_create``: safe to call repeatedly (bootstrap data
    migration, ops scripts) without duplicating groups or clobbering existing
    memberships. Permission objects for the capability identifiers do not
    exist yet; they are wired up by the later permissions micro-tasks.
    """
    return {role: Group.objects.get_or_create(name=role)[0] for role in STAFF_ROLES}
