"""TIER1-THROTTLE-CACHE: the DRF throttle count must be ONE count, not one per worker.

DRF's SimpleRateThrottle keeps its per-identity request history on the
`default` cache alias, and LocMemCache is per-process: with N gunicorn
workers each worker kept its own window, so the coupon scope's 30/min
admitted roughly N*30/min. The fix points `CACHES['default']` at Django's
own RedisCache when REDIS_URL is declared in the environment, and leaves
the per-process LocMemCache in place when it is not - a bare local dev
and a Redis-less CI need no new service to boot.

Two halves are pinned here, and the split is deliberate:

- The CONFIG pins (always run, need no server) boot settings in a
  subprocess and observe which backend the default alias resolves to for
  each shape of the environment. Importing settings opens no socket, so
  "REDIS_URL declared -> RedisCache" is pinned without a Redis anywhere.
- The SHARED-COUNT pins (need a reachable Redis; they SKIP - loudly,
  naming the env var - when none is declared or reachable, because the
  suite must stay runnable without one) drive the real ScopedRateThrottle
  and prove the budget is spent once for the deployment: two independent
  cache connections - the two-worker case - and a separately SPAWNED OS
  process - the true multi-process case - both read and extend the same
  history, and the 30/min budget admits 30 requests in total, not 30 per
  process. The numbers there are hand-written literals: a pin whose
  expected value is parsed out of the rate string would agree with a
  wrong rate from both sides.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache, caches
from django.core.cache import cache as default_cache
from django.core.cache.backends.redis import RedisCache
from django.test import SimpleTestCase
from rest_framework.settings import api_settings
from rest_framework.test import APIRequestFactory
from rest_framework.throttling import ScopedRateThrottle

from tests.test_settings_security import _DEV_ENV, run_settings_import

BACKEND_DIR = Path(__file__).resolve().parent.parent

# The literal budget under test, written by hand rather than derived from
# RATE_UNDER_TEST: a recomputed oracle is not an oracle.
BUDGET = 30
RATE_UNDER_TEST = f"{BUDGET}/min"
# TEST-NET-3: never a real caller, stable across the parent and child legs.
IDENT_IP = "203.0.113.9"


def _explicit_redis_url():
    """REDIS_URL exactly as the operator declared it, or None.

    settings already ran load_dotenv() at import, so a developer's .env
    counts as a declaration; a declared server that then refuses to
    connect is a skip in the shared-count pins, never a hang.
    """
    return (os.environ.get("REDIS_URL") or "").strip() or None


def _redis_reachable(url):
    """(ok, reason). Three-second connect bound so an absent server is a
    SKIP rather than a hung suite; any failure counts as absent."""
    import redis

    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
        client.ping()
        client.close()
        return True, ""
    except Exception as exc:  # refused / DNS / timeout - all "not armed"
        return False, f"{type(exc).__name__}: {exc}"


class ThrottleCacheConfigTests(SimpleTestCase):
    """The default cache alias is env-driven: RedisCache when REDIS_URL is
    declared, per-process LocMemCache when it is not. Subprocess-pinned
    because CACHES is built at settings-import time, so an in-process read
    would prove only what this run happened to import."""

    _REDIS_URL_UNDER_TEST = "redis://127.0.0.1:6390/14"
    _PRINT_DEFAULT = (
        "import config.settings as s; "
        "print('DEFAULT', s.CACHES['default']['BACKEND']); "
        "print('LOCATION', s.CACHES['default']['LOCATION'])"
    )

    def test_without_redis_url_the_default_alias_stays_per_process(self):
        res = run_settings_import(dict(_DEV_ENV), snippet=self._PRINT_DEFAULT)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(
            "DEFAULT django.core.cache.backends.locmem.LocMemCache", res.stdout
        )
        self.assertIn("LOCATION default", res.stdout)

    def test_with_redis_url_the_default_alias_resolves_to_redis(self):
        res = run_settings_import(
            {**_DEV_ENV, "REDIS_URL": self._REDIS_URL_UNDER_TEST},
            snippet=self._PRINT_DEFAULT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("DEFAULT django.core.cache.backends.redis.RedisCache", res.stdout)
        self.assertIn(f"LOCATION {self._REDIS_URL_UNDER_TEST}", res.stdout)

    def test_an_empty_redis_url_falls_back_to_the_per_process_cache(self):
        """`.env.example` keys get copied around; a stray bare `REDIS_URL=`
        must not hand RedisCache an empty URL - the `or default` shape
        keeps the documented local-dev fallback."""
        res = run_settings_import(
            {**_DEV_ENV, "REDIS_URL": ""},
            snippet=self._PRINT_DEFAULT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(
            "DEFAULT django.core.cache.backends.locmem.LocMemCache", res.stdout
        )

    def test_cache_backend_override_still_wins_over_the_redis_resolution(self):
        """CACHE_BACKEND / CACHE_LOCATION predate this switch and remain
        the escape hatch for a deployment that wants a different store."""
        res = run_settings_import(
            {
                **_DEV_ENV,
                "REDIS_URL": self._REDIS_URL_UNDER_TEST,
                "CACHE_BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "CACHE_LOCATION": "operator-override",
            },
            snippet=self._PRINT_DEFAULT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(
            "DEFAULT django.core.cache.backends.locmem.LocMemCache", res.stdout
        )
        self.assertIn("LOCATION operator-override", res.stdout)


class DefaultAliasWiringTests(SimpleTestCase):
    """In-process companion: whatever this run launched with, the alias the
    throttles read must be the one the environment declared. The
    class-level cache DRF binds is the module-level default-cache PROXY,
    which resolves the alias per access - that is exactly why re-pointing
    CACHES['default'] re-points every throttle."""

    def test_scoped_throttle_reads_the_default_cache_proxy(self):
        self.assertIs(ScopedRateThrottle.cache, default_cache)

    def test_default_alias_backend_matches_the_redis_declaration(self):
        expected = "RedisCache" if _explicit_redis_url() else "LocMemCache"
        self.assertEqual(caches["default"].__class__.__name__, expected)


class SharedThrottleCountTests(SimpleTestCase):
    """The behavioural half: with a Redis actually reachable, the coupon
    budget is spent ONCE for the deployment. Each test skips - loudly,
    naming the env var - when no Redis is declared or reachable, because
    the suite must stay runnable without one; the skip is an honest
    "not armed here", never a silent pass."""

    def setUp(self):
        super().setUp()
        url = _explicit_redis_url()
        if not url:
            self.skipTest(
                "REDIS_URL is not declared in this environment; set it to a "
                "reachable Redis to arm the cross-process throttle pins"
            )
        ok, reason = _redis_reachable(url)
        if not ok:
            self.skipTest(f"REDIS_URL is declared but no Redis answered ({reason})")
        self.redis_url = url
        # The suite's own throttle windows are reset per ApiTestCase; this
        # class is a SimpleTestCase, so it resets the alias it writes to.
        cache.clear()

    def _anon_request(self):
        request = APIRequestFactory().post("/api/orders/apply-coupon/")
        request.META["REMOTE_ADDR"] = IDENT_IP
        request.user = AnonymousUser()
        return request

    def _rates(self):
        rates = dict(api_settings.DEFAULT_THROTTLE_RATES)
        rates["coupon"] = RATE_UNDER_TEST
        return rates

    def test_two_workers_share_one_budget_not_one_budget_each(self):
        """Worker A spends the whole 30/min budget through its own cache
        connection; worker B - a SEPARATE RedisCache instance, its own
        connection pool, the two-gunicorn-worker case - is refused the
        very next request. Per-process caches would hand B a fresh budget
        of 30 (90 in total across three workers): the exact gap this task
        closes."""
        worker_a = RedisCache(self.redis_url, {})
        worker_b = RedisCache(self.redis_url, {})
        view = SimpleNamespace(throttle_scope="coupon")
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", self._rates()):
            throttle_a = ScopedRateThrottle()
            throttle_a.cache = worker_a
            throttle_b = ScopedRateThrottle()
            throttle_b.cache = worker_b
            request = self._anon_request()
            allowed = sum(
                throttle_a.allow_request(request, view) for _ in range(BUDGET)
            )
            self.assertEqual(allowed, BUDGET, "worker A's own budget must hold")
            # A's spent history is visible to B through the shared backend:
            # every one of B's attempts is refused, none refills the window.
            b_results = [throttle_b.allow_request(request, view) for _ in range(5)]
            self.assertEqual(b_results, [False] * 5)
            self.assertFalse(throttle_a.allow_request(request, view))
            observed = worker_b.get(throttle_a.key)
            self.assertEqual(len(observed), BUDGET)

    def test_count_is_shared_with_a_separately_spawned_process(self):
        """The pin above shares one OS; this one shares only the server: a
        child process - its own Python, its own settings import, its own
        cache connections - spends from the SAME budget, and the parent's
        remaining allowance shrinks by exactly the child's spend."""
        parent = ScopedRateThrottle()
        parent.cache = RedisCache(self.redis_url, {})
        view = SimpleNamespace(throttle_scope="coupon")
        child_script = (
            "import django, os\n"
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
            "django.setup()\n"
            "from types import SimpleNamespace\n"
            "from unittest import mock\n"
            "from django.contrib.auth.models import AnonymousUser\n"
            "from django.core.cache.backends.redis import RedisCache\n"
            "from rest_framework.settings import api_settings\n"
            "from rest_framework.test import APIRequestFactory\n"
            "from rest_framework.throttling import ScopedRateThrottle\n"
            "rates = dict(api_settings.DEFAULT_THROTTLE_RATES)\n"
            "rates['coupon'] = os.environ['THROTTLE_PIN_RATE']\n"
            "with mock.patch.object(ScopedRateThrottle, 'THROTTLE_RATES', rates):\n"
            "    throttle = ScopedRateThrottle()\n"
            "    throttle.cache = RedisCache(os.environ['REDIS_URL'], {})\n"
            "    request = APIRequestFactory().post('/api/orders/apply-coupon/')\n"
            "    request.META['REMOTE_ADDR'] = os.environ['THROTTLE_PIN_IP']\n"
            "    request.user = AnonymousUser()\n"
            "    view = SimpleNamespace(throttle_scope='coupon')\n"
            "    print('CHILD_ALLOWED', throttle.allow_request(request, view))\n"
        )
        with mock.patch.object(ScopedRateThrottle, "THROTTLE_RATES", self._rates()):
            self.assertTrue(parent.allow_request(self._anon_request(), view))
            res = subprocess.run(
                [sys.executable, "-c", child_script],
                capture_output=True,
                text=True,
                cwd=tempfile.gettempdir(),
                env={
                    **os.environ,
                    "THROTTLE_PIN_RATE": RATE_UNDER_TEST,
                    "THROTTLE_PIN_IP": IDENT_IP,
                    "PYTHONPATH": str(BACKEND_DIR),
                },
                timeout=90,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("CHILD_ALLOWED True", res.stdout)
            # One spend belongs to the parent and one to the child, so the
            # parent has exactly BUDGET - 2 left: the child's request
            # counted against the parent's window. Without sharing the
            # parent would still see BUDGET - 1 remaining.
            allowed = sum(
                parent.allow_request(self._anon_request(), view) for _ in range(BUDGET)
            )
            self.assertEqual(allowed, BUDGET - 2)
            self.assertFalse(parent.allow_request(self._anon_request(), view))
