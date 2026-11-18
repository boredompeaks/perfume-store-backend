import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache as default_cache
from django.core.cache import caches
from django.core.cache.backends.redis import RedisCache
from django.test import SimpleTestCase
from rest_framework.settings import api_settings
from rest_framework.test import APIRequestFactory
from rest_framework.throttling import ScopedRateThrottle

from tests.test_settings_security import _DEV_ENV, run_settings_import

BACKEND_DIR = Path(__file__).resolve().parent.parent

# The literal budget under test: every assertion compares against this
# hand-written number, never a value parsed back out of the rate string.
BUDGET = 30
RATE_UNDER_TEST = f"{BUDGET}/min"
IDENT_IP = "203.0.113.9"


def _explicit_redis_url():
    """settings already ran load_dotenv() at import, so a developer's .env counts as a declaration; an unreachable server is a skip, never a hang."""
    return (os.environ.get("REDIS_URL") or "").strip() or None


def _redis_reachable(url):
    import redis

    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
        client.ping()
        client.close()
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


class ThrottleCacheConfigTests(SimpleTestCase):
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
        res = run_settings_import(
            {**_DEV_ENV, "REDIS_URL": ""},
            snippet=self._PRINT_DEFAULT,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(
            "DEFAULT django.core.cache.backends.locmem.LocMemCache", res.stdout
        )

    def test_cache_backend_override_still_wins_over_the_redis_resolution(self):
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
    def test_scoped_throttle_reads_the_default_cache_proxy(self):
        self.assertIs(ScopedRateThrottle.cache, default_cache)

    def test_default_alias_backend_matches_the_redis_declaration(self):
        expected = "RedisCache" if _explicit_redis_url() else "LocMemCache"
        self.assertEqual(caches["default"].__class__.__name__, expected)


class SharedThrottleCountTests(SimpleTestCase):
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
        # This SimpleTestCase writes to the explicit operator DB these workers share, not the proxy.
        RedisCache(self.redis_url, {}).clear()

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
        """Worker B is a separate RedisCache instance (the two-gunicorn-worker case) and is refused the next request; per-process caches would hand it a fresh budget."""
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
            b_results = [throttle_b.allow_request(request, view) for _ in range(5)]
            self.assertEqual(b_results, [False] * 5)
            self.assertFalse(throttle_a.allow_request(request, view))
            observed = worker_b.get(throttle_a.key)
            self.assertEqual(len(observed), BUDGET)

    def test_count_is_shared_with_a_separately_spawned_process(self):
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
            allowed = sum(
                parent.allow_request(self._anon_request(), view) for _ in range(BUDGET)
            )
            self.assertEqual(allowed, BUDGET - 2)
            self.assertFalse(parent.allow_request(self._anon_request(), view))
