from django import forms
from django.contrib import admin
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied

from common.admin import RoleAwareModelAdmin
from common.roles import STAFF_ROLES
from orders.models import Order
# Replace auth's default User admin with the store-aware one below.
admin.site.unregister(User)


class StoreUserChangeForm(UserChangeForm):
    """UserChangeForm plus the role-scoped staff management field.

    The raw ``groups`` M2M stays out of the fieldsets deliberately: the
    roles surface (spec 6.12, [6.12.1]) manages exactly the six staff role
    Groups, so the form exposes only those — a plain Groups selector would
    let even an admin grant unmapped, non-role authority. Any other group
    membership is not this surface's business and survives saves here.
    """

    staff_roles = forms.ModelMultipleChoiceField(
        queryset=Group.objects.none(),
        required=False,
        label="Staff roles",
        help_text="The six staff roles from the RBAC map (spec 6.12). "
        "Assigning or removing them requires the staff.manage capability.",
        widget=FilteredSelectMultiple("Staff roles", is_stacked=False),
    )

    def clean(self):
        cleaned_data = super().clean()
        roles = cleaned_data.get("staff_roles")
        target_is_staff = cleaned_data.get("is_staff", self.instance.is_staff)
        if roles and not target_is_staff:
            # Role groups grant staff API authority; a non-staff account
            # holding one is an escalation channel — the user could never
            # reach the admin again to have it revoked.
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


@admin.register(User)
class StoreUserAdmin(RoleAwareModelAdmin, DjangoUserAdmin):
    """Customer view: who they are, what they've ordered — plus the staff
    roles management surface ([6.12.1]).

    Role-aware least privilege (spec 6.12): anyone with ``customers.read``
    may inspect customer records, but the User row is also where
    ``is_staff`` and the role groups live — viewing/creating/editing/
    deleting it is privilege management, so every mutation rides
    ``staff.manage`` (admin only). This base class must precede
    ``DjangoUserAdmin`` so the capability-driven permission methods win
    the MRO.

    The roles field renders only for ``staff.manage`` holders on staff
    users (hidden otherwise, least privilege); every Group add/remove it
    causes writes its own LogEntry naming the acting user ([6.12.5]).
    """

    form = StoreUserChangeForm
    capability_map = {
        "view": "customers.read",
        "add": "staff.manage",
        "change": "staff.manage",
        "delete": "staff.manage",
    }
    # DjangoUserAdmin's fieldsets with the raw groups M2M swapped for the
    # role-scoped field; the add view keeps DjangoUserAdmin.add_fieldsets,
    # which never offered group assignment either (roles are assigned on
    # the change page after the account exists).
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
        # Least privilege (spec 6.12): the roles section exists only for
        # staff.manage holders, and only on staff users. Stripping it here
        # removes it from the rendered form, the POSTed form and the
        # read-only view alike (the class-level fieldsets are rebuilt, not
        # mutated — per-request state must never leak across requests).
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
        # Note: as a declared field, staff_roles rides in every built form
        # even when the fieldsets strip it — that is safe because a viewer
        # without the capability gets 403 on POST (change gate), the
        # save_model guard refuses off-flow callers, and clean() rejects
        # roles on non-staff targets. Seeding stays capability-gated so
        # stripped renders never expose even the choices list.
        if obj is not None and self._map_grants(request, "change") and obj.is_staff:
            # Exactly the six role groups (never arbitrary Groups), seeded
            # with the user's current role membership.
            form.base_fields["staff_roles"].queryset = Group.objects.filter(
                name__in=STAFF_ROLES
            ).order_by("name")
            form.base_fields["staff_roles"].initial = obj.groups.filter(
                name__in=STAFF_ROLES
            )
        return form

    # Spec 6.12 (line 2261): a non-superuser editor — the admin role
    # included — may reassign the six staff roles but must never grant the
    # user flags themselves. is_staff opens the admin door, is_superuser is
    # the trust anchor, and user_permissions is the same "permission
    # change" escalation channel. Read-only keeps the current values
    # visible while ModelForm excludes the fields entirely, so a crafted
    # POST is ignored rather than validated away — only the superuser
    # bypass grants flags. Role assignment (staff.manage) is unaffected.
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
            # Defense in depth against unauthorized privilege escalation
            # (spec 6.12): the field only renders for staff.manage holders,
            # so role data reaching this point means the caller bypassed
            # the view flow — refuse before the user row is written at all.
            raise PermissionDenied(
                "Role changes require the staff.manage capability."
            )
        super().save_model(request, obj, form, change)
        self._apply_staff_roles(request, obj, form)

    def _apply_staff_roles(self, request, obj, form):
        """Sync the six staff role groups, auditing each add/remove.

        The diff runs only across the role groups — any other group
        membership is not this surface's business and survives the save.
        Each direction logs separately via ``log_change``, which records
        the acting user (``request.user``) and the role changed ([6.12.5]).
        """
        if "staff_roles" not in form.cleaned_data:
            return
        desired = set(form.cleaned_data["staff_roles"])
        held = {g for g in obj.groups.all() if g.name in STAFF_ROLES}
        for group in sorted(held - desired, key=lambda g: g.name):
            obj.groups.remove(group)
            self.log_change(request, obj, f'Removed role "{group.name}".')
        for group in sorted(desired - held, key=lambda g: g.name):
            obj.groups.add(group)
            self.log_change(request, obj, f'Added role "{group.name}".')

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
