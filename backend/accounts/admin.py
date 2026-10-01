from django import forms
from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.admin.widgets import FilteredSelectMultiple
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied
from django.urls import reverse

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

# Replace auth's default User admin with the store-aware one below.
admin.site.unregister(User)

# SPEC-20-2 [R-20.18]: a staff-role add/remove is a privilege grant and gets
# the same interstitial the destructive bulk actions use — same template, same
# confirm contract, one mechanism. The prompt is deliberately not the bulk
# action's: unlike a cancel, a role change CAN be undone, so telling the
# operator otherwise would be a lie on the screen.
ROLE_CHANGE_ACTION = "staff_role_change"
ROLE_CHANGE_WARNING = (
    "adds or removes staff authority on this account. Confirm to continue."
)


class MFAAdminAuthenticationForm(AdminAuthenticationForm):
    """Admin login + mandatory TOTP for privileged roles (SPEC-17-05).

    The second enforcement surface for R-17.9: ``is_privileged`` users
    (superusers and ``staff.manage`` holders) must present a valid TOTP
    code in the login form, and an unenrolled privileged account is
    refused with the enrollment path named — same semantics as the staff
    API login. ``user_cache`` is set only after the password validates, so
    MFA errors appear only for correct credentials (nothing is leaked to
    a caller who failed the first factor). Non-privileged staff and
    customers log in exactly as before — the field is optional and unused
    for them.

    SPEC-20-8b: a device this browser was trusted on skips the fresh code
    here too, through :func:`accounts.mfa_trust.is_trusted_device` — the
    SAME decision the staff API serializer makes, so the directive "don't
    challenge me every login" holds on the primary staff door and there is
    exactly one implementation of the rule to keep honest. ``AdminSite.login``
    hands the request to ``LoginView``, which passes it to the form, so
    ``self.request.COOKIES`` is where the signed marker lives. Everything
    below the skip is the unchanged pre-SPEC-20-8 challenge.
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
        cleaned_data = super().clean()
        user = getattr(self, "user_cache", None)
        if user is None or not is_privileged(user):
            return cleaned_data
        device = TOTPDevice.active_for(user)
        if device is None:
            raise forms.ValidationError(MFA_ENROLLMENT_REQUIRED)
        # The shared skip decision — see the class docstring. Read before
        # the code is demanded, and never instead of the enrollment check
        # above: a disabled factor must not be answered by a stale grant.
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

    The raw ``groups`` M2M stays out of the fieldsets deliberately: the
    roles surface (spec 6.12, [6.12.1]) manages exactly the staff role
    Groups, so the form exposes only those — a plain Groups selector would
    let even an admin grant unmapped, non-role authority. Any other group
    membership is not this surface's business and survives saves here.

    SPEC-1-B03: the queryset it is seeded with is narrowed per caller by
    ``StoreUserAdmin.get_form``, so a caller who may not assign a role is
    never even offered it (least privilege); the write path refuses such a
    submission regardless.
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
    ``staff.manage`` (admin, and the superadmin tier above it). This base
    class must precede ``DjangoUserAdmin`` so the capability-driven
    permission methods win the MRO.

    The roles field renders only for ``staff.manage`` holders on staff
    users (hidden otherwise, least privilege); every Group add/remove it
    causes writes its own LogEntry naming the acting user ([6.12.5]).

    SPEC-1-B03 adds a second, narrower gate on that field: assigning the
    ``superadmin`` role is itself a high-privilege access change (spec 1.1
    line 146), so it needs ``platform.configure`` — which ``admin`` does not
    hold. ``_assignable_roles`` hides the tier from callers who cannot grant
    it, and ``_refuse_restricted_roles`` refuses the submission itself.
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
            # Exactly the role groups (never arbitrary Groups), seeded
            # with the user's current role membership. SPEC-1-B03: the
            # queryset is narrowed to the roles this caller may actually
            # assign, so the top tier is not even offered to an Admin that
            # could not commit it (least privilege — the refusal below is
            # the authority, this is what keeps the offer honest).
            form.base_fields["staff_roles"].queryset = Group.objects.filter(
                name__in=self._assignable_roles(request)
            ).order_by("name")
            form.base_fields["staff_roles"].initial = obj.groups.filter(
                name__in=STAFF_ROLES
            )
        return form

    # ——— the escalation guard on role assignment (SPEC-1-B03) ———

    def _assignable_roles(self, request):
        """Role names this caller may add or remove on the roles field."""
        return tuple(
            role for role in STAFF_ROLES if user_may_assign_role(request.user, role)
        )

    def _restricted_roles(self, request, groups):
        """The subset of ``groups`` this caller may not add or remove."""
        return sorted(
            group.name
            for group in groups
            if not user_may_assign_role(request.user, group.name)
        )

    def _refuse_restricted_roles(self, request, groups):
        """Refuse a role change the caller has no authority to make.

        The check is on the DIFF, both directions: minting the top tier is an
        escalation and so is unminting it, and an Admin may do neither. It
        raises rather than silently dropping the role from the submission,
        because a caller who asked for a change they cannot make deserves the
        refusal, not a save that quietly did something else.
        """
        for role in self._restricted_roles(request, groups):
            raise PermissionDenied(
                f'Assigning the "{role}" role requires the '
                f"{ROLE_GRANT_CAPABILITY[role]} capability."
            )

    # Spec 6.12 (line 2261): a non-superuser editor — the admin role
    # included — may reassign the staff roles it holds authority over but
    # must never grant the user flags themselves. is_staff opens the admin
    # door, is_superuser is the trust anchor, and user_permissions is the
    # same "permission change" escalation channel. Read-only keeps the
    # current values visible while ModelForm excludes the fields entirely,
    # so a crafted POST is ignored rather than validated away — only the
    # superuser bypass grants flags. Role assignment (staff.manage) is
    # unaffected; SPEC-1-B03 gates the TOP TIER separately, on
    # platform.configure, which an admin-role editor does not hold.
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
            raise PermissionDenied("Role changes require the staff.manage capability.")
        super().save_model(request, obj, form, change)
        self._apply_staff_roles(request, obj, form)

    def _apply_staff_roles(self, request, obj, form):
        """Sync the staff role groups, auditing each add/remove.

        The diff runs only across the role groups — any other group
        membership is not this surface's business and survives the save.
        Each direction logs separately via ``log_change``, which records
        the acting user (``request.user``) and the role changed ([6.12.5]).

        SPEC-1-B03: the diff is checked against the escalation guard before
        a single membership row moves, so this holds for the confirmed
        commit leg and for any caller that reaches the sync off the view
        flow — an Admin can never mint or unmint the tier above itself.

        SPEC-20-4 [R-20.27]/[R-20.29]: those per-direction messages are
        prose, so they cannot answer what the account's authority was before
        and after. One structured event records the real before -> after role
        sets and states the surface as ``admin``; the per-direction LogEntry
        rows are unchanged, so the shipped audit contract still holds.
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
        # The changeform view wraps this save in its own atomic block, so the
        # event commits with the group membership or rolls back with it.
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

    # ——— SPEC-20-2 [R-20.18]: the explicit confirm step ———

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        # The seam of the shipped view flow (the same method ModelAdmin's
        # get_urls wraps), entered before the form is even validated: a
        # staff-role change must be confirmed, and this is the only place
        # that can stop the save before save_model/_apply_staff_roles run.
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
        """The role add/remove this POST would commit, as a detail payload,
        or ``None`` when there is nothing to confirm.

        ``None`` means the ordinary view flow runs. The check is a DIFF, never
        a presence test: an emptied multi-select legitimately submits no
        ``staff_roles`` key at all, which is exactly a removal — so a
        presence test would be the bypass.

        Three guards come before the diff, each for a reason:

        - the commit leg returns ``None``: ``confirm=yes`` means the user
          already confirmed this exact submission and the normal, fully
          validated save must now run — which is where the SPEC-1-B03
          escalation guard refuses a role change it has no authority for;
        - a caller without ``staff.manage`` returns ``None``: it must be
          refused by the view's own change gate, never shown a confirmation
          page for a privilege change they may not make;
        - a non-staff target returns ``None``: roles are not part of a
          customer's change surface at all (``get_fieldsets``), so that
          form keeps its own guards and its own pins.

        The confirmation covers the whole submission, not just the role rows,
        because re-driving the same payload through the ordinary save is what
        keeps every existing form guard in force on the commit leg.
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
        # The interstitial REPORTS the diff; it does not validate it. Only
        # the role groups are resolved (the rendered choices), and a
        # non-numeric or foreign pk is simply not part of the diff — the
        # form's own queryset validation stays the authority on the commit
        # leg, so a crafted value can never 500 the confirmation or, worse,
        # be reported as if it were a role.
        submitted = {pk for pk in request.POST.getlist("staff_roles") if pk.isdigit()}
        desired = set(Group.objects.filter(pk__in=submitted, name__in=STAFF_ROLES))
        added = sorted(desired - held, key=lambda group: group.name)
        removed = sorted(held - desired, key=lambda group: group.name)
        # SPEC-1-B03: a change this caller may not commit gets a refusal, not
        # a confirmation — asking an Admin to confirm granting the tier above
        # itself, only to be refused after they click, would be theatre.
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
