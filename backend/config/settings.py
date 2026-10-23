"""
Django settings for config project.

No database is configured: this service is stateless (one request in,
one analysis out). If a future requirement needs persistence (e.g. caching
weather responses across requests), add it deliberately then — don't carry
an unused sqlite file for "architectural completeness".
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="django-insecure-dev-only-change-in-prod")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS", default=["http://localhost:5173"]
)

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "corsheaders",
    "api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves STATIC_ROOT directly from the app process — this service has no
    # separate static-file host, and the only static assets are drf-spectacular's
    # Swagger/Redoc UI bundles.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # Placed near the top so its response-phase compression runs last, after
    # every other middleware has finished shaping the body — otherwise it
    # would compress a response that later middleware still mutates. Earns
    # its keep specifically on /api/routes/analyze/: the heatmap payload
    # (up to ~99 points x 49 hours x 4 metrics) is the largest response this
    # API returns and compresses well, being mostly repeated JSON keys and
    # small numbers.
    "django.middleware.gzip.GZipMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

WSGI_APPLICATION = "config.wsgi.application"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# "routes" gets its own LocMemCache instance, separate from "default"
# (used by weather_client.py). They were sharing one cache and that was a
# real bug, not a hypothetical one: the corridor heatmap alone can write
# 1,500+ weather entries in a single request, which blew past
# LocMemCache's default MAX_ENTRIES=300 and silently evicted the route
# cache entry within the same request that wrote it — verified live, not
# assumed, via debug logging during Phase 4a. Weather's write volume and
# routes' "write once, reuse for an hour" pattern don't belong in the same
# small eviction pool.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    },
    "routes": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "routes-cache",
        "OPTIONS": {"MAX_ENTRIES": 1000},
    },
}

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # No user accounts in this app (django.contrib.auth isn't installed) —
    # this is a single, keyless, public endpoint for the project's own
    # frontend. Without this, DRF's request.user still tries to import
    # django.contrib.auth.models.AnonymousUser and breaks at request time.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Weather-Aware Truck Routing API",
    "DESCRIPTION": "Route alternatives, weather-checkpoint sampling, and risk-based recommendation for truck routing.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # level/wind_level/rain_level/.../wind_level_after_load_rule all reuse the
    # same RiskLevel choice set; without this, drf-spectacular mints a
    # differently-named enum component per field.
    "ENUM_NAME_OVERRIDES": {
        "RiskLevelEnum": "api.serializers.RISK_LEVEL_CHOICES",
    },
}

# --- External service configuration ---
# Values come from the environment; see .env.example for every key this
# project reads. OSRM and Nominatim need no API key at all.
OSRM_BASE_URL = env("OSRM_BASE_URL", default="https://router.project-osrm.org")
NOMINATIM_BASE_URL = env("NOMINATIM_BASE_URL", default="https://nominatim.openstreetmap.org")
OPEN_METEO_BASE_URL = env(
    "OPEN_METEO_BASE_URL", default="https://api.open-meteo.com/v1/forecast"
)
