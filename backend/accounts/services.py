"""Account erasure that respects the financial-record retention boundary.

``Order.user`` keeps the order tied to an account, so deleting an account
that owns one either cascades the order away or is refused by the database.
Neither is an acceptable erasure: the order, its payment references and its
refunds are financial records that must outlive the login. Erasure therefore
tombstones the account row in place - the identifying fields are replaced,
the login is made unusable, and the row's primary key keeps every order
attached to it.

The audit entry states who performed the erasure and why, and deliberately
carries none of the values being erased: the old email, username and names
are the data the request asked to remove.
"""

from django.contrib.auth import get_user_model
from django.db import transaction

from common.models import AuditEvent

# RFC 2606 reserves ``.invalid`` as a guaranteed-non-resolvable TLD, and the
# account pk makes the local part unique per tombstone. The username twin is
# unique for the same reason (``User.username`` is unique).
_TOMBSTONE_EMAIL = "erased-{pk}@invalid"
_TOMBSTONE_USERNAME = "erased-{pk}"

# ``AuditEvent.record`` resolves the row's category from this prefix. The
# event-type vocabulary on ``AuditEvent.EventType`` is a model field's
# choices, so naming a new member here would be a schema change; the prefix
# is what the write path actually needs to classify the event.
_ERASED_EVENT_TYPE = "auth.account_erased"

_ERASED_FIELDS = (
    "username",
    "email",
    "first_name",
    "last_name",
    "is_active",
    "password",
)


class AccountAlreadyErased(ValueError):
    """The account already carries the erasure tombstone."""


def _tombstone_email(pk):
    return _TOMBSTONE_EMAIL.format(pk=pk)


def _tombstone_username(pk):
    return _TOMBSTONE_USERNAME.format(pk=pk)


def _is_erased(account):
    # Both markers, not either one: the pair is what this module writes and
    # what makes a second call a refusal rather than a new erasure. A row
    # with only one of them was not tombstoned by this function, so it is
    # completed instead of being mistaken for a finished erasure.
    return account.username == _tombstone_username(
        account.pk
    ) and account.email == _tombstone_email(account.pk)


@transaction.atomic
def erase_account(user, *, actor=None, reason=""):
    """Tombstone ``user`` in place and return the erased row.

    The row is never deleted, so no ``Order``/``Refund``/``PaymentEvent``
    cascades or blocks on the retained owner. The row lock serialises two
    concurrent erasure requests: the second one sees the tombstone and is
    refused with :class:`AccountAlreadyErased` instead of appending a second
    audit row.

    ``source`` is ``SYSTEM`` because a service call carries no request whose
    surface could be named; attribution rides the ``actor`` foreign key.
    """
    if user.pk is None:
        raise ValueError("Cannot erase an account that has never been saved.")

    User = get_user_model()
    account = User.objects.select_for_update().get(pk=user.pk)
    if _is_erased(account):
        raise AccountAlreadyErased("This account is already erased.")

    account.username = _tombstone_username(account.pk)
    account.email = _tombstone_email(account.pk)
    account.first_name = ""
    account.last_name = ""
    account.is_active = False
    account.set_unusable_password()
    account.save(update_fields=_ERASED_FIELDS)

    AuditEvent.record(
        _ERASED_EVENT_TYPE,
        actor=actor,
        detail={
            "action": "account.erased",
            "user_id": account.pk,
            "reason": reason,
        },
        source=AuditEvent.Source.SYSTEM,
    )
    return account
