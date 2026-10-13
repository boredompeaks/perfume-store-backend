"""The Celery application: the repo's broker, worker and scheduler.

SPEC-2-03 / SPEC-19-1 (ASYNC-INFRA). This is the repo's FIRST worker of any
kind, and it exists because the audit found a hole no amount of substrate code
could close: ``expire_reservations``, ``drain_notification_outbox`` and
``purge_notification_outbox`` were all built, all tested, and none of them
SCHEDULED. A reservation sweep nobody runs means stale holds gate stock
forever; an outbox nobody drains means a queued notification is written and
never sent. Both were real, and both are one scheduler away from fixed.

This module deliberately contains NO policy: no cadence, no broker URL, no
schedule entry. Every one of those is a deployment-shaped value with a
constraint attached to it, so every one is env-driven and documented in
``config.settings`` / ``.env.example`` (where the reason for each default is
written next to it). What lives here is only the wiring that makes
``celery -A config worker`` work, which is a fact about the package layout and
not a choice any deployment should make.

**This module converts no call site; the call sites converted themselves**
(TIER1-ASYNC-CONVERT turned ``orders/events.py``,
``orders/views/payment.py`` and ``orders/webhooks.py`` to ``enqueue``).
The ordering hazard that dictated the sequencing is historical and stays
recorded: converting a site before a drain is proven means a notification
that is silently never delivered. The worker landed first, against a drain
this commit made real; the sites followed once that drain was audited.

**Redis is the default broker, and it is never contacted at import.** Building
the app resolves configuration; it opens no socket. The test suite therefore
imports this module in an environment with no Redis running at all, which is
what lets the suite stay green there. Celery connects only when a task is
actually published or a worker actually starts.
"""

import os

from celery import Celery

# The Django settings module, set the standard way so `celery -A config` and a
# bare `import config` find the same settings. `manage.py` sets it too;
# setdefault (not assignment) so a worker started with an explicit
# DJANGO_SETTINGS_MODULE for a settings override keeps it.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# The app NAME is the config package, which is what makes `celery -A config`
# resolve: celery looks for a module attribute of that name, and
# `config/__init__.py` re-exports this app as `celery_app` alongside the `app`
# alias below.
app = Celery("config")

# Every CELERY_* setting in config.settings becomes app configuration - the
# broker, the result backend, the serializer, and the beat schedule. Passing
# the settings MODULE (not the imported module object) is what keeps this
# lazy: celery reads each key when it is first needed rather than at import,
# so a settings override in a test is honoured and no broker connection is
# opened here. `namespace="CELERY"` is what makes the mapping
# CELERY_BROKER_URL -> broker_url rather than a literal prefix match on every
# key.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Task discovery. The sweeps live in `common/tasks.py`, and `common` is an
# installed app, so celery's per-app `tasks` lookup finds them without this
# module importing them. Autodiscovery is LAZY (it runs when the app is
# finalized), which is deliberate: importing `config.celery` from
# `config/__init__.py` therefore does not drag task modules into every
# management command, and a task module that fails to import surfaces at worker
# boot - where it is diagnosable - rather than in the middle of a migration.
app.autodiscover_tasks()
