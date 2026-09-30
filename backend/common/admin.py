"""Role-aware Django admin base (spec 6.12, permission enforcement).

The Django admin surface previously relied on Django's own model
permissions, which none of the six staff roles carry — in practice only
the superuser could work the admin. This base routes the four model
permissions (view/add/change/delete) through ``CAPABILITY_ROLES`` instead,
so each staff role sees exactly the models its function needs
(least privilege, [6.12.7]) and the ``admin`` role — which the roles map
grants every capability — keeps full function.

Two conventions guardrails are honoured deliberately:

- Authorization data comes solely from the roles map; the only user-flag
  shortcut here is Django's own superuser bypass, which is preserved so
  the trust anchor keeps working exactly as before.
- Bulk/export actions are gated through ``get_actions`` (the same seam
  Django uses for ``allowed_permissions``), so a gated action disappears
  from the dropdown *and* is rejected if POSTed directly.

Destructive bulk actions ([6.12.4] — explicit confirmation for sensitive
actions) declare themselves in ``confirmation_required_actions``; the base
interposes a confirmation interstitial between the dropdown POST and the
action body. Actions that already render their own deliberate input form
(e.g. adjust-stock) are their own confirmation and stay off the list.

SPEC-20-1 [R-20.20] enriches that one interstitial rather than adding a
second mechanism: a financially significant action supplies a *structured*
detail payload (``confirmation_details`` — amount, currency, affected items,
resulting state) and the shared template renders it above the object list.
SPEC-20-2 [R-20.18] then reuses the very same page as the confirm step for a
mutation that is not a bulk action (a staff-role change on the user change
form): the interstitial re-drives the submission it interrupted, so there is
one confirmation contract, one template and one commit gate.
"""
from dataclasses import replace
from functools import wraps

from django.contrib import admin
from django.contrib.admin.options import ActionLocation
from django.shortcuts import render
from django.urls import reverse

from common.permissions import user_has_capability

# The POST contract of the interstitial. Module constants, not inline
# literals, because three places have to agree on them: the render path
# (emits them), the commit gate (refuses to act without the confirm) and the
# re-submission path for a non-bulk confirmation.
CONFIRM_FIELD = "confirm"
CONFIRMATION_YES = "yes"

# The prompt the delivered bulk-action interstitial has always shown, kept
# verbatim as the default so rendering one stays byte-identical to before.
DEFAULT_CONFIRM_WARNING = (
    "is a sensitive action and cannot be undone. Confirm to continue."
)


def _repost_fields(post):
    """Flatten the interrupted POST into ``(name, value)`` hidden pairs.

    A non-bulk confirmation must re-drive the *same* submission, so every
    field the user already filled travels through the interstitial and the
    confirmed POST re-runs the ordinary, fully validated save — the
    interstitial adds a step, never a second, looser write path. Multi-valued
    fields contribute one pair per value. The CSRF token (the template
    renders a fresh one) and the confirm marker (the template owns it) are
    dropped, so an interrupted POST can neither forge a token nor pin its own
    outcome. ``request.FILES`` is deliberately not flattened: no file field
    rides this seam, and re-sending bytes through hidden inputs is not a thing
    to invent for it.
    """
    return tuple(
        (name, value)
        for name, values in post.lists()
        if name != CONFIRM_FIELD and not name.startswith("csrf")
        for value in values
    )


class RoleAwareModelAdmin(admin.ModelAdmin):
    """ModelAdmin whose permissions flow from ``CAPABILITY_ROLES``.

    Subclasses declare:

    - ``capability_map``: permission kind -> capability identifier. A kind
      mapped to ``None`` (or absent) is not grantable to any staff role —
      only the superuser bypass can perform it (e.g. orders are created by
      checkout, never from the admin).
    - ``action_capabilities``: bulk action name -> capability identifier.
      Every entry in ``ModelAdmin.actions`` must be mapped here; the test
      suite pins that invariant so a new action cannot ship ungated.
    - ``confirmation_required_actions``: bulk action names that must not
      execute until the user explicitly confirms on the interstitial page.
    """

    capability_map = {}
    action_capabilities = {}
    confirmation_required_actions = frozenset()

    # ——— capability plumbing ———

    def _holds_capability(self, request, capability):
        """Superuser bypass first (Django's own, preserved verbatim),
        then the roles map; unknown capability ids deny by default."""
        user = request.user
        if user.is_superuser:
            return True
        return user_has_capability(user, capability)

    def _map_grants(self, request, kind):
        if request.user.is_superuser:
            return True
        capability = self.capability_map.get(kind)
        return capability is not None and user_has_capability(request.user, capability)

    # ——— the four model permissions, driven by the map ———

    def has_view_permission(self, request, obj=None):
        return self._map_grants(request, "view")

    def has_add_permission(self, request):
        return self._map_grants(request, "add")

    def has_change_permission(self, request, obj=None):
        return self._map_grants(request, "change")

    def has_delete_permission(self, request, obj=None):
        return self._map_grants(request, "delete")

    def has_module_permission(self, request):
        # The default checks Django model permissions (which no role
        # carries), which would hide every model from the admin index;
        # module visibility must follow the same map as the model itself.
        return self.has_view_permission(request)

    # ——— privileged-action audit trail ([6.12.5]) ———

    def log_bulk_action(self, request, queryset, message):
        # Change-form saves and deletes leave a LogEntry automatically, but
        # queryset bulk actions bypass save_model entirely — without this,
        # the privileged paths gated above would mutate rows unlogged.
        for obj in queryset:
            self.log_change(request, obj, message)

    # ——— bulk / export action gating + confirmation ———

    def get_actions(self, request, action_location=ActionLocation.CHANGE_LIST):
        # Same seam Django uses for ``allowed_permissions``: filtering here
        # hides the action from the dropdown and makes a directly POSTed
        # action name an invalid choice, so the gate holds on both paths.
        actions = super().get_actions(request, action_location)
        gated = {}
        for name, action in actions.items():
            capability = self.action_capabilities.get(name)
            if capability is not None and not self._holds_capability(
                request, capability
            ):
                continue
            if name in self.confirmation_required_actions:
                action = replace(action, func=self._confirm_first(action))
            gated[name] = action
        return gated

    def _confirm_first(self, action):
        """Wrap an unbound action so the first POST renders the
        confirmation interstitial instead of executing."""
        func = action.func

        @wraps(func)
        def confirmed_first(admin_self, request, queryset):
            # The superuser keeps the legacy direct execution: the bypass
            # is Django's own trust anchor and predates RBAC.
            if (
                request.user.is_superuser
                or request.POST.get(CONFIRM_FIELD) == CONFIRMATION_YES
            ):
                return func(admin_self, request, queryset)
            return admin_self.render_action_confirmation(request, action, queryset)

        return confirmed_first

    # ——— the interstitial's structured detail payload (SPEC-20-1) ———

    def confirmation_details(self, request, action_name, objects):
        """The structured payload the interstitial renders above the objects.

        Empty by default: an action with nothing consequential to spell out
        keeps the delivered description/count/object-list render untouched.
        A financially significant action overrides this and supplies the
        amount and its currency, the affected items and the resulting state
        per selected row (SPEC-20-1 [R-20.20]).
        """
        return {}

    def render_confirmation(
        self,
        request,
        *,
        action_name,
        description,
        objects,
        details=None,
        warning=DEFAULT_CONFIRM_WARNING,
        re_post=False,
        cancel_url=None,
    ):
        """Render the interstitial — the single confirmation surface.

        Bulk actions and non-bulk mutations (the staff-role change step)
        both come through here, so there is one template, one confirm
        contract and one place the structured detail payload is assembled.
        """
        return render(
            request,
            "admin/action_confirmation.html",
            {
                "title": description,
                "description": description,
                "warning": warning,
                "action_name": action_name,
                "objects": objects,
                "select_across": request.POST.get("select_across") == "1",
                "opts": self.model._meta,
                "details": (
                    details
                    if details is not None
                    else self.confirmation_details(request, action_name, objects)
                ),
                "pending_fields": _repost_fields(request.POST) if re_post else (),
                "cancel_url": cancel_url or reverse("admin:index"),
            },
        )

    def render_action_confirmation(self, request, action, queryset):
        return self.render_confirmation(
            request,
            action_name=action.name,
            description=action.description,
            objects=queryset,
        )