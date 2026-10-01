"""Staff role definitions for RBAC (spec 6.12, tiers per spec 1.1).

This module is the single source of truth for staff roles and their
capabilities. Later micro-tasks (permissions.py, staff admin endpoints)
resolve authorization checks against ``CAPABILITY_ROLES`` instead of
re-hardcoding role names, and Django Groups are synced from ``STAFF_ROLES``
so group membership can back the role assignment.

Spec 1.1 (lines 133-150) ends the role table with two tiers rather than one:
Admin "manage[s] users, roles, settings and operational access" (line 137)
and Superadmin "manage[s] high-privilege settings, access and platform
configuration" (line 146), under the rule that "admin is not one giant
permission". So the top tier is NOT admin-plus-everything: it shares admin's
two privilege-management capabilities and adds one of its own,
``platform.configure``, which admin deliberately does not hold. It holds no
operational capability at all — the spec grants the tier none.
"""

from django.contrib.auth.models import Group

ROLE_SUPPORT = "support"
ROLE_CATALOGUE = "catalogue"
ROLE_INVENTORY = "inventory"
ROLE_MARKETING = "marketing"
ROLE_FINANCE = "finance"
ROLE_ADMIN = "admin"
ROLE_SUPERADMIN = "superadmin"

# Stable, ordered tuple of the seven staff roles (spec 6.12; the top tier
# from spec 1.1 lines 142-146). The order is the tier order: the six
# operational roles first, the tier above them last.
STAFF_ROLES = (
    ROLE_SUPPORT,
    ROLE_CATALOGUE,
    ROLE_INVENTORY,
    ROLE_MARKETING,
    ROLE_FINANCE,
    ROLE_ADMIN,
    ROLE_SUPERADMIN,
)

# Capability identifier (spec 6.12 examples) -> roles granted that capability.
# Least privilege by default: each role holds only what its function needs,
# and only the two privilege tiers hold the sensitive staff/settings
# capabilities, so role changes themselves stay a privileged concern.
CAPABILITY_ROLES = {
    "products.read": frozenset(
        {ROLE_SUPPORT, ROLE_CATALOGUE, ROLE_INVENTORY, ROLE_MARKETING, ROLE_ADMIN}
    ),
    "products.write": frozenset({ROLE_CATALOGUE, ROLE_ADMIN}),
    "products.publish": frozenset({ROLE_CATALOGUE, ROLE_ADMIN}),
    "inventory.read": frozenset({ROLE_INVENTORY, ROLE_CATALOGUE, ROLE_ADMIN}),
    "inventory.adjust": frozenset({ROLE_INVENTORY, ROLE_ADMIN}),
    "orders.read": frozenset({ROLE_SUPPORT, ROLE_FINANCE, ROLE_ADMIN}),
    # Spec 1.1 line 110 puts packing and shipping on the inventory/fulfilment
    # operator ("Manage stock, packing, shipping and returns"), so it shares
    # the fulfilment capability with support (line 92, "permitted order
    # issues") rather than holding it alone. That capability is exactly the
    # order-status walk pending->confirmed->shipped->delivered, which is the
    # packing/shipping authority and nothing more: orders.read (order
    # visibility), orders.cancel, refunds.create and customers.read stay with
    # the roles spec 1.1 names them for.
    "orders.fulfill": frozenset({ROLE_SUPPORT, ROLE_INVENTORY, ROLE_ADMIN}),
    "orders.cancel": frozenset({ROLE_SUPPORT, ROLE_ADMIN}),
    "refunds.create": frozenset({ROLE_FINANCE, ROLE_ADMIN}),
    "customers.read": frozenset({ROLE_SUPPORT, ROLE_FINANCE, ROLE_ADMIN}),
    "discounts.write": frozenset({ROLE_MARKETING, ROLE_ADMIN}),
    "reports.read": frozenset({ROLE_FINANCE, ROLE_MARKETING, ROLE_ADMIN}),
    # The two privilege-management capabilities of spec 1.1: access (role and
    # staff management) and settings. Admin holds both per line 137, and the
    # top tier holds them too per line 146 ("access and high-privilege
    # settings") — that is what makes it a tier above admin rather than a
    # separate label.
    "staff.manage": frozenset({ROLE_ADMIN, ROLE_SUPERADMIN}),
    "settings.manage": frozenset({ROLE_ADMIN, ROLE_SUPERADMIN}),
    # Spec 1.1 line 146: platform configuration. Granted to the top tier and
    # to no other role, which is what stops an Admin from minting or
    # unminting the tier above itself (see ROLE_GRANT_CAPABILITY).
    "platform.configure": frozenset({ROLE_SUPERADMIN}),
}

# Roles whose own grant/revoke is a high-privilege access change, mapped to
# the capability that authorises it. Minting or unminting the top tier is
# spec 1.1 line 146 authority ("platform configuration"), so it needs a
# capability ``admin`` does not hold: an Admin manages staff roles through
# ``staff.manage`` but can never promote anyone — or itself — above its own
# tier. Django's ``is_superuser`` stays the bypass, as on every admin
# surface (see ``common.permissions.user_may_assign_role``).
ROLE_GRANT_CAPABILITY = {ROLE_SUPERADMIN: "platform.configure"}


def sync_role_groups():
    """Create the staff role groups, returning them keyed by role name.

    Idempotent via ``get_or_create``: safe to call repeatedly (bootstrap data
    migrations, ops scripts) without duplicating groups or clobbering existing
    memberships. Permission objects for the capability identifiers do not
    exist yet; they are wired up by the later permissions micro-tasks.
    """
    return {role: Group.objects.get_or_create(name=role)[0] for role in STAFF_ROLES}
