import os
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent
env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env")
if env("OUTBOUND_NO_PROXY", default=""):
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = env("OUTBOUND_NO_PROXY")
SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
INSTALLED_APPS = [
    "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions",
    "django.contrib.messages", "django.contrib.staticfiles", "rest_framework", "drf_spectacular",
    "apps.common.apps.CommonConfig", "apps.identity", "apps.markets", "apps.wallets",
    "apps.trading", "apps.alerts", "apps.events", "apps.audit", "apps.operations",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.common.middleware.RequestContextMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]
ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"
AUTH_USER_MODEL = "identity.User"
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": BASE_DIR / env("DATABASE_PATH", default="stockman.sqlite3"),
    "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
    "TEST": {"NAME": BASE_DIR / "test-stockman.sqlite3"},
}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "EXCEPTION_HANDLER": "apps.common.api.exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_RATES": {"login": env("LOGIN_RATE", default="10/min")},
}
SPECTACULAR_SETTINGS = {
    "TITLE": "Stockman API", "DESCRIPTION": "Audited spot paper trading. No real money.",
    "VERSION": "1.0.0", "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("ar", "العربية"), ("de", "Deutsch"), ("es", "Español")]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True
USE_TZ = True
TIME_ZONE = "UTC"
STATIC_URL = "static/"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = env.bool("SECURE_COOKIES", default=not DEBUG)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SESSION_COOKIE_AGE = 86400
CSRF_FAILURE_VIEW = "apps.common.api.csrf_failure"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
BINANCE_BASE_URL = env("BINANCE_BASE_URL", default="https://data-api.binance.vision")
BINANCE_SYMBOLS = env.list("BINANCE_SYMBOLS", default=["BTCUSDT", "ETHUSDT"])
MARKET_POLL_SECONDS = env.int("MARKET_POLL_SECONDS", default=10)
QUOTE_MAX_AGE_SECONDS = env.int("QUOTE_MAX_AGE_SECONDS", default=30)
MARKET_ORDER_TIMEOUT_SECONDS = env.int("MARKET_ORDER_TIMEOUT_SECONDS", default=60)
EODHD_BASE_URL = env("EODHD_BASE_URL", default="https://eodhd.com/api")
EODHD_API_TOKEN = env("EODHD_API_TOKEN", default="")
EGX_ACCESS_VERIFIED = env.bool("EGX_ACCESS_VERIFIED", default=False)
EGX_SYMBOLS = env.list("EGX_SYMBOLS", default=[])
EODHD_DAILY_CALL_LIMIT = env.int("EODHD_DAILY_CALL_LIMIT", default=20)
EGX_PUBLICATION_GRACE_SECONDS = env.int("EGX_PUBLICATION_GRACE_SECONDS", default=3600)
SSE_POLL_SECONDS = env.float("SSE_POLL_SECONDS", default=2)
SSE_HEARTBEAT_SECONDS = env.float("SSE_HEARTBEAT_SECONDS", default=15)
EVENT_RETENTION_DAYS = env.int("EVENT_RETENTION_DAYS", default=7)
MARKET_HISTORY_DAYS = env.int("MARKET_HISTORY_DAYS", default=30)
ALERT_COOLDOWN_SECONDS = env.int("ALERT_COOLDOWN_SECONDS", default=300)
WORKER_LEASE_SECONDS = env.int("WORKER_LEASE_SECONDS", default=120)
