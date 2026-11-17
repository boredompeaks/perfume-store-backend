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
are the data the request asked to remove. The operator's free-text reason is
validated before anything is written, for the same reason: a reason that
repeats the old identity would re-insert it into an append-only row and the
log mirror.

The saved delivery addresses are not financial records, so they are deleted
in the same transaction as the tombstone rather than blanked and kept.
"""

import re

from django.contrib.auth import get_user_model
from django.db import transaction

from accounts.models import Address
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

# The reason is written to an append-only audit row and mirrored to the log,
# so it is checked against the values the erasure is about to remove. Both
# patterns are deliberately broad: a false refusal costs a rewritten reason,
# a miss cannot be undone.
_EMAIL_IN_REASON = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Separators are ignored between digits, so "98765-00299" and "98765 00299"
# are the same ten digits. The register's phone fields hold 10-15 digits, so
# a shorter run is an ordinary number (a ticket id, an amount).
_PHONE_IN_REASON = re.compile(r"(?:\+?\d[\s().\-]*){10,}")

_UNSAFE_REASON_MESSAGE = (
    "The erasure reason carries personal data: an identifier of this account, "
    "an email address or a phone number. Rewrite it without them - the reason "
    "is kept in an append-only audit row."
)


class AccountAlreadyErased(ValueError):
    """The account already carries the erasure tombstone."""


class UnsafeErasureReason(ValueError):
    """The reason would re-insert the identity that erasure removes."""


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


def _reason_is_unsafe(reason, account):
    """True when the operator's reason carries the identity being erased.

    The account's own values are matched by containment rather than by word:
    any occurrence at all leaks them into a row that can never be edited, and
    a word-boundary rule misses a run-together name or an address embedded in
    prose. The patterns cover a contact address naming ANY person, not only
    this account holder, because the row is append-only either way.
    """
    haystack = reason.casefold()
    identifiers = (
        account.email,
        account.username,
        account.first_name,
        account.last_name,
        f"{account.first_name} {account.last_name}",
    )
    if any(
        value.strip().casefold() in haystack for value in identifiers if value.strip()
    ):
        return True
    return bool(_EMAIL_IN_REASON.search(reason) or _PHONE_IN_REASON.search(reason))


@transaction.atomic
def erase_account(user, *, actor=None, reason=""):
    """Tombstone ``user`` in place and return the erased row.

    The row is never deleted, so no ``Order``/``Refund``/``PaymentEvent``
    cascades or blocks on the retained owner. The addresses the account owns
    ARE destroyed in the same transaction: they are PII the request covers
    and no financial record references them. The row lock serialises two
    concurrent erasure requests: the second one sees the tombstone and is
    refused with :class:`AccountAlreadyErased` instead of appending a second
    audit row.

    A ``reason`` that carries the identity being erased is refused with
    :class:`UnsafeErasureReason` before the tombstone is written, because the
    reason lands in an append-only row and the log mirror.

    ``source`` is ``SYSTEM`` because a service call carries no request whose
    surface could be named; attribution rides the ``actor`` foreign key.
    """
    if user.pk is None:
        raise ValueError("Cannot erase an account that has never been saved.")

    User = get_user_model()
    account = User.objects.select_for_update().get(pk=user.pk)
    if _is_erased(account):
        raise AccountAlreadyErased("This account is already erased.")

    # Before the tombstone: a rejected reason must leave nothing behind, and
    # the values it is checked against are the ones this call is replacing.
    if _reason_is_unsafe(reason, account):
        raise UnsafeErasureReason(_UNSAFE_REASON_MESSAGE)

    account.username = _tombstone_username(account.pk)
    account.email = _tombstone_email(account.pk)
    account.first_name = ""
    account.last_name = ""
    account.is_active = False
    account.set_unusable_password()
    account.save(update_fields=_ERASED_FIELDS)

    # Destroyed, not blanked-and-kept: the address is the customer's, the
    # order does not reference it, and the erasure request covers it.
    Address.objects.filter(user_id=account.pk).delete()

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
