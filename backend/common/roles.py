"""Staff role definitions and the single capability map for RBAC.

It is the one source of truth for roles and capabilities; Django Groups are synced
from ``STAFF_ROLES``.
"""

from django.contrib.auth.models import Group

ROLE_SUPPORT = "support"
ROLE_CATALOGUE = "catalogue"
ROLE_INVENTORY = "inventory"
ROLE_MARKETING = "marketing"
ROLE_FINANCE = "finance"
ROLE_ADMIN = "admin"
ROLE_SUPERADMIN = "superadmin"

# The seven staff roles in tier order: six operational roles, then the two
# privilege tiers.
STAFF_ROLES = (
    ROLE_SUPPORT,
    ROLE_CATALOGUE,
    ROLE_INVENTORY,
    ROLE_MARKETING,
    ROLE_FINANCE,
    ROLE_ADMIN,
    ROLE_SUPERADMIN,
)

# capability -> roles granted it. Least privilege by default: only the two
# privilege tiers hold the staff/settings capabilities.
CAPABILITY_ROLES = {
    "products.read": frozenset(
        {ROLE_SUPPORT, ROLE_CATALOGUE, ROLE_INVENTORY, ROLE_MARKETING, ROLE_ADMIN}
    ),
    "products.write": frozenset({ROLE_CATALOGUE, ROLE_ADMIN}),
    "products.publish": frozenset({ROLE_CATALOGUE, ROLE_ADMIN}),
    "inventory.read": frozenset({ROLE_INVENTORY, ROLE_CATALOGUE, ROLE_ADMIN}),
    "inventory.adjust": frozenset({ROLE_INVENTORY, ROLE_ADMIN}),
    "orders.read": frozenset({ROLE_SUPPORT, ROLE_FINANCE, ROLE_ADMIN}),
    # Spec 1.1 puts packing/shipping on the inventory operator, so fulfilment is
    # shared with support; order visibility, cancellation and refunds stay with
    # the roles named for them.
    "orders.fulfill": frozenset({ROLE_SUPPORT, ROLE_INVENTORY, ROLE_ADMIN}),
    "orders.cancel": frozenset({ROLE_SUPPORT, ROLE_ADMIN}),
    # A rate is money every future order will be charged, so it follows
    # finance/admin rather than the packing operator: working a shipment and
    # setting its price are different powers.
    "shipping.manage": frozenset({ROLE_FINANCE, ROLE_ADMIN}),
    # Working a shipment and its tracking trail is support/admin work; the
    # inventory operator is deliberately excluded because B03 pinned its set.
    # Read/write are split so a read-only role can take the trail without the pen.
    "shipments.read": frozenset({ROLE_SUPPORT, ROLE_ADMIN}),
    "shipments.write": frozenset({ROLE_SUPPORT, ROLE_ADMIN}),
    # Return review is support/admin work, split read/write because a return row
    # reaches a customer's order and address. Deliberately NOT granted to the
    # inventory operator: B03 pinned that role's capability set, and widening it
    # would undo a shipped contract.
    "returns.read": frozenset({ROLE_SUPPORT, ROLE_ADMIN}),
    "returns.write": frozenset({ROLE_SUPPORT, ROLE_ADMIN}),
    "refunds.create": frozenset({ROLE_FINANCE, ROLE_ADMIN}),
    "customers.read": frozenset({ROLE_SUPPORT, ROLE_FINANCE, ROLE_ADMIN}),
    "discounts.write": frozenset({ROLE_MARKETING, ROLE_ADMIN}),
    "reports.read": frozenset({ROLE_FINANCE, ROLE_MARKETING, ROLE_ADMIN}),
    # Access (role/staff management) and settings: admin holds both, and the
    # superadmin tier holds them plus platform configuration.
    "staff.manage": frozenset({ROLE_ADMIN, ROLE_SUPERADMIN}),
    "settings.manage": frozenset({ROLE_ADMIN, ROLE_SUPERADMIN}),
    # Granted only to the top tier, which stops an Admin from granting the tier
    # above itself (see ROLE_GRANT_CAPABILITY).
    "platform.configure": frozenset({ROLE_SUPERADMIN}),
}

# Roles whose grant is a high-privilege access change, mapped to the capability
# that authorises it, so an Admin can never promote above its own tier. Django's
# ``is_superuser`` bypass remains (see ``common.permissions.user_may_assign_role``).
ROLE_GRANT_CAPABILITY = {ROLE_SUPERADMIN: "platform.configure"}


def sync_role_groups():
    """Create the staff role groups, keyed by role name; idempotent via
    ``get_or_create``."""
    return {role: Group.objects.get_or_create(name=role)[0] for role in STAFF_ROLES}
