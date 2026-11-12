from django import forms
from django.conf import settings
from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from rest_framework.throttling import SimpleRateThrottle

from common import totp
from common.admin import CONFIRM_FIELD, CONFIRMATION_YES, RoleAwareModelAdmin
from common.audit import log_mutation
from common.models import AuditEvent
from common.permissions import is_privileged, user_may_assign_role
from common.roles import ROLE_GRANT_CAPABILITY, STAFF_ROLES
from orders.models import Order

from . import mfa_trust
from .models import (
    MFA_CODE_INVALID,
    MFA_CODE_REQUIRED,
    MFA_ENROLLMENT_REQUIRED,
    TOTPDevice,
)

admin.site.unregister(User)

# A staff-role change is a privilege grant, so it gets the same confirm
# interstitial as the destructive bulk actions; its wording says it can be undone.
ROLE_CHANGE_ACTION = "staff_role_change"
ROLE_CHANGE_WARNING = (
    "adds or removes staff authority on this account. Confirm to continue."
)

# Generic on purpose: it names no account, ident rule or remaining time, so the
# refusal is identical regardless of who the caller tried to be.
ADMIN_LOGIN_THROTTLE_MESSAGE = (
    "Too many failed authentication attempts. Try again shortly."
)


class AdminLoginThrottle(SimpleRateThrottle):
    """Failed-attempt budget for the Django admin login form.

    DRF's throttles only see DRF views, so the API 'auth' budget never covered
    this plain-Django door; this reuses the same cache-window primitive, keyed
    per client ident and charged only on failed attempts. The rate is the
    env-driven ``ADMIN_LOGIN_THROTTLE_RATE`` knob (empty disables it).
    """

    scope = "admin-login"

    def get_rate(self):
        return getattr(settings, "ADMIN_LOGIN_THROTTLE_RATE", None) or None

    def get_cache_key(self, request, view):
        # request is the plain HttpRequest the form carries; get_ident reads only
        # headers/META. The view is unused but kept to match the base-class signature.
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }

    def _window(self, request):
        """(key, now, history) for this ident, history pruned to the window."""
        key = self.get_cache_key(request, None)
        now = self.timer()
        history = self.cache.get(key, [])
        while history and history[-1] <= now - self.duration:
            history.pop()
        return key, now, history

    def blocked(self, request):
        """True when this ident has spent its failed-attempt budget."""
        if self.rate is None:
            return False
        _, _, history = self._window(request)
        return len(history) >= self.num_requests

    def record_failure(self, request):
        """Spend one unit of the budget (one failed admin login)."""
        if self.rate is None:
            return
        key, now, history = self._window(request)
        history.insert(0, now)
        self.cache.set(key, history, self.duration)


class MFAAdminAuthenticationForm(AdminAuthenticationForm):
    """Admin login plus mandatory TOTP for privileged roles.

    Privileged users must present a valid code (an unenrolled account is refused
    with the enrollment path named), and a browser trusted through
    ``accounts.mfa_trust`` skips the challenge via the same decision the staff API
    makes. MFA errors appear only after the password validates, so nothing leaks
    to a caller who failed the first factor.
    """

    totp = forms.CharField(
        label="Authentication code",
        required=False,
        widget=forms.TextInput(
            attrs={
                "inputmode": "numeric",
                "autocomplete": "one-time-code",
            }
        ),
    )

    def clean(self):
        """Password + MFA, wrapped in the failed-attempt budget.

        The budget is checked before the password is evaluated and spent on
        every ValidationError, so it bounds failures, never honest traffic; a
        form built without a request (as the tests do) skips the throttle.
        """
        request = getattr(self, "request", None)
        throttle = AdminLoginThrottle()
        if request is not None and throttle.blocked(request):
            raise forms.ValidationError(ADMIN_LOGIN_THROTTLE_MESSAGE)
        try:
            return self._clean_with_mfa()
        except forms.ValidationError:
            if request is not None:
                throttle.record_failure(request)
            raise

    def _clean_with_mfa(self):
        cleaned_data = super().clean()
        user = getattr(self, "user_cache", None)
        if user is None or not is_privileged(user):
            return cleaned_data
        device = TOTPDevice.active_for(user)
        if device is None:
            raise forms.ValidationError(MFA_ENROLLMENT_REQUIRED)
        # The shared skip decision, read only after the enrollment check:
        # a stale grant must not answer a disabled factor.
        if mfa_trust.is_trusted_device(getattr(self, "request", None), user, device):
            return cleaned_data
        code = cleaned_data.get("totp")
        if not code:
            raise forms.ValidationError(MFA_CODE_REQUIRED)
        counter = totp.verify_code(
            device.secret,
            code,
            at_time=totp.now(),
            last_used_counter=device.last_used_counter,
        )
        if counter is None:
            raise forms.ValidationError(MFA_CODE_INVALID)
        # RFC 6238 §5.2: consume the counter so the same code can never
        # log in twice.
        device.last_used_counter = counter
        device.save(update_fields=["last_used_counter"])
        return cleaned_data


class StoreUserChangeForm(UserChangeForm):
    """UserChangeForm plus the role-scoped staff management field.

    The raw ``groups`` M2M stays out deliberately: only the staff role Groups
    are exposed, so an admin cannot grant unmapped authority, and other group
    memberships survive saves. The queryset is narrowed per caller, so a caller
    who may not assign a role is never offered it.
    """

    staff_roles = forms.ModelMultipleChoiceField(
        queryset=Group.objects.none(),
        required=False,
        label="Staff roles",
        help_text="The staff roles from the RBAC map (spec 1.1). "
        "Assigning or removing them requires the staff.manage capability; "
        "the superadmin role additionally requires platform.configure.",
        widget=FilteredSelectMultiple("Staff roles", is_stacked=False),
    )

    def clean(self):
        cleaned_data = super().clean()
        roles = cleaned_data.get("staff_roles")
        target_is_staff = cleaned_data.get("is_staff", self.instance.is_staff)
        if roles and not target_is_staff:
            # Role groups grant staff API authority; on a non-staff account that
            # is an escalation channel.
            raise forms.ValidationError(
                "Staff roles can only be assigned to staff users."
            )
        return cleaned_data


class OrderInline(admin.TabularInline):
    model = Order
    extra = 0
    can_delete = False
    fields = ("id", "status", "total_amount", "created_at")
    readonly_fields = ("id", "status", "total_amount", "created_at")
    verbose_name = "Order"
    verbose_name_plural = "Orders (newest first)"
    ordering = ("-created_at",)

    def has_add_permission(self, request, obj=None):
        return False


# RoleAwareModelAdmin must come first so its capability methods win the MRO.
@admin.register(User)
class StoreUserAdmin(RoleAwareModelAdmin, DjangoUserAdmin):
    """Customer view plus the staff-roles management surface.

    Mutating a User row is privilege management, so every mutation rides
    ``staff.manage``; the roles field renders only for holders on staff users,
    and assigning the ``superadmin`` tier additionally needs
    ``platform.configure``.
    """

    form = StoreUserChangeForm
    capability_map = {
        "view": "customers.read",
        "add": "staff.manage",
        "change": "staff.manage",
        "delete": "staff.manage",
    }
    # DjangoUserAdmin's fieldsets with the raw groups M2M swapped for the
    # role-scoped field; the add view keeps add_fieldsets, which never offered groups.
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "staff_roles",
                    "user_permissions",
                ),
            },
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    list_display = (
        "username",
        "email",
        "order_count",
        "spent_total",
        "is_active",
        "is_staff",
        "date_joined",
    )
    list_filter = ("is_active", "is_staff", "date_joined")
    search_fields = ("username", "email", "first_name", "last_name")
    inlines = (OrderInline,)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        # Least privilege: the roles section exists only for staff.manage holders
        # on staff users; rebuild the tuple, never mutate class state.
        if obj is not None and not (
            self._map_grants(request, "change") and obj.is_staff
        ):
            return tuple(
                (
                    name,
                    {
                        key: (
                            tuple(f for f in value if f != "staff_roles")
                            if key == "fields"
                            else value
                        )
                        for key, value in options.items()
                    },
                )
                for name, options in fieldsets
            )
        return fieldsets

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        # A declared field rides in every built form even when stripped here; the
        # POST gate and save_model guard refuse any off-flow use, so that is safe.
        if obj is not None and self._map_grants(request, "change") and obj.is_staff:
            # Exactly the role groups, narrowed to what this caller may assign:
            # the refusal below is the authority, this keeps the offer honest.
            form.base_fields["staff_roles"].queryset = Group.objects.filter(
                name__in=self._assignable_roles(request)
            ).order_by("name")
            form.base_fields["staff_roles"].initial = obj.groups.filter(
                name__in=STAFF_ROLES
            )
        return form

    def _assignable_roles(self, request):
        return tuple(
            role for role in STAFF_ROLES if user_may_assign_role(request.user, role)
        )

    def _restricted_roles(self, request, groups):
        return sorted(
            group.name
            for group in groups
            if not user_may_assign_role(request.user, group.name)
        )

    def _refuse_restricted_roles(self, request, groups):
        """Refuse a role change the caller has no authority to make.

        The check is on the diff in both directions — unminting the top tier is
        an escalation too — and it raises rather than silently dropping the role.
        """
        for role in self._restricted_roles(request, groups):
            raise PermissionDenied(
                f'Assigning the "{role}" role requires the '
                f"{ROLE_GRANT_CAPABILITY[role]} capability."
            )

    # Non-superusers may reassign roles but never grant user flags: is_staff
    # opens the admin door and is_superuser is the trust anchor. Read-only keeps
    # them visible while excluding them from the form, so a crafted POST is ignored.
    PRIVILEGE_FLAGS = ("is_staff", "is_superuser", "user_permissions")

    def get_readonly_fields(self, request, obj=None):
        readonly = super().get_readonly_fields(request, obj)
        if obj is not None and not request.user.is_superuser:
            return tuple(readonly) + self.PRIVILEGE_FLAGS
        return readonly

    def save_model(self, request, obj, form, change):
        if "staff_roles" in form.cleaned_data and not self._map_grants(
            request, "change"
        ):
            # Defense in depth: role data reaching here means the caller bypassed
            # the view flow.
            raise PermissionDenied("Role changes require the staff.manage capability.")
        super().save_model(request, obj, form, change)
        self._apply_staff_roles(request, obj, form)

    def _apply_staff_roles(self, request, obj, form):
        """Sync the staff role groups, auditing each add/remove.

        Only role groups are diffed (other memberships survive), the escalation
        guard runs before a single row moves, and one structured event records
        the before/after sets alongside the per-direction LogEntry rows.
        """
        if "staff_roles" not in form.cleaned_data:
            return
        desired = set(form.cleaned_data["staff_roles"])
        held = {g for g in obj.groups.all() if g.name in STAFF_ROLES}
        self._refuse_restricted_roles(request, held ^ desired)
        for group in sorted(held - desired, key=lambda g: g.name):
            obj.groups.remove(group)
            self.log_change(request, obj, f'Removed role "{group.name}".')
        for group in sorted(desired - held, key=lambda g: g.name):
            obj.groups.add(group)
            self.log_change(request, obj, f'Added role "{group.name}".')
        if held == desired:
            return
        # The changeform view wraps this save in an atomic block, so the event
        # commits with the membership or rolls back with it.
        log_mutation(
            request,
            obj,
            AuditEvent.EventType.STAFF_ROLES_UPDATED,
            "staff_roles_updated",
            AuditEvent.Source.ADMIN,
            changes=[
                (
                    "staff_roles",
                    sorted(group.name for group in held),
                    sorted(group.name for group in desired),
                )
            ],
        )

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        # The seam of the shipped view flow, entered before the form is
        # validated: the only place that can stop the save before save_model runs.
        pending = self._pending_role_change(request, object_id)
        if pending is not None:
            return self.render_confirmation(
                request,
                action_name=ROLE_CHANGE_ACTION,
                description=f"Change staff roles for {pending['username']}",
                objects=[pending["user"]],
                details=pending,
                warning=ROLE_CHANGE_WARNING,
                re_post=True,
                cancel_url=self._change_url(pending["user"]),
            )
        return super().changeform_view(request, object_id, form_url, extra_context)

    def _change_url(self, obj):
        """The change page the interstitial's Cancel link returns to."""
        meta = self.model._meta
        return reverse(
            f"admin:{meta.app_label}_{meta.model_name}_change", args=[obj.pk]
        )

    def _pending_role_change(self, request, object_id):
        """The role add/remove this POST would commit, as a detail payload, or None.

        The check is a diff, never a presence test (an emptied multi-select sends
        no key at all, and that is a removal); the commit leg, callers without
        ``staff.manage`` and non-staff targets return None so the ordinary view
        flow keeps its own guards.
        """
        if request.method != "POST" or object_id is None:
            return None
        if request.POST.get(CONFIRM_FIELD) == CONFIRMATION_YES:
            return None
        if not self._map_grants(request, "change"):
            return None
        target = self.get_object(request, object_id)
        if target is None or not target.is_staff:
            return None
        held = set(target.groups.filter(name__in=STAFF_ROLES))
        # The interstitial reports the diff, it does not validate it: the form's
        # queryset stays the authority on the commit leg.
        submitted = {pk for pk in request.POST.getlist("staff_roles") if pk.isdigit()}
        desired = set(Group.objects.filter(pk__in=submitted, name__in=STAFF_ROLES))
        added = sorted(desired - held, key=lambda group: group.name)
        removed = sorted(held - desired, key=lambda group: group.name)
        # A change this caller may not commit gets the refusal now, not a
        # confirmation followed by one.
        self._refuse_restricted_roles(request, set(added) | set(removed))
        if not (added or removed):
            return None
        return {
            "user": target,
            "username": target.username,
            "summary": (
                f"{len(added)} role(s) to add, {len(removed)} to remove — each is "
                "audited separately under your name once confirmed"
            ),
            "rows": [
                {
                    "label": target.username,
                    "fields": [
                        ("Roles to add", self._role_names(added)),
                        ("Roles to remove", self._role_names(removed)),
                    ],
                    "items": [],
                }
            ],
        }

    @staticmethod
    def _role_names(groups):
        return ", ".join(group.name for group in groups) or "none"

    @admin.display(description="Orders")
    def order_count(self, obj):
        return obj.orders.count()

    @admin.display(description="Spent")
    def spent_total(self, obj):
        from django.db.models import Sum

        total = obj.orders.filter(
            status__in=("confirmed", "shipped", "delivered")
        ).aggregate(s=Sum("total_amount"))["s"]
        return f"₹{total}" if total is not None else "₹0"


@admin.register(TOTPDevice)
class TOTPDeviceAdmin(RoleAwareModelAdmin):
    """Read-only register of enrolled MFA factors, answering whether an account
    has a working second factor without touching a database console.

    Nothing is writable, even through the superuser bypass: a hand-edited row
    could strip or plant a factor, and enrollment/disable have their own audited
    flows. ``secret`` appears in no field, column, filter or search target.
    """

    capability_map = {"view": "customers.read"}
    fields = (
        "user",
        "enabled",
        "confirmed_at",
        "last_used_counter",
        "trusted_until",
        "created_at",
    )
    # Pinning readonly to the view-only page's fields keeps even a superuser
    # from typing into it.
    readonly_fields = fields
    list_display = ("user", "enabled", "confirmed_at", "trusted_until", "created_at")
    list_filter = ("enabled",)
    search_fields = ("user__username", "user__email")
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
