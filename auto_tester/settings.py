import django_stubs_ext

django_stubs_ext.monkeypatch()

from pathlib import Path

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent

# Security
SECRET_KEY: str = config("SECRET_KEY", cast=str)
DEBUG: bool = config("DEBUG", default=False, cast=bool)
ALLOWED_HOSTS: list[str] = config(
    "ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv()
)

# Reverse proxy / HTTPS
SECURE_PROXY_SSL_HEADER: tuple[str, str] = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True
CSRF_TRUSTED_ORIGINS: list[str] = config(
    "CSRF_TRUSTED_ORIGINS", default="https://sench.remotereps.com", cast=Csv()
)

# Application definition
INSTALLED_APPS: list[str] = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "channels",
    "django_celery_beat",
    "django_celery_results",
    # Local
    "accounts",
    "dashboard",
    "projects",
    "agents",
]

MIDDLEWARE: list[str] = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "auto_tester.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "auto_tester.wsgi.application"
ASGI_APPLICATION = "auto_tester.asgi.application"

# Database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": config("DB_NAME", cast=str),
        "USER": config("DB_USER", cast=str),
        "PASSWORD": config("DB_PASSWORD", cast=str),
        "HOST": config("DB_HOST", cast=str),
        "PORT": config("DB_PORT", default="5432", cast=str),
    }
}

# Auth
AUTH_USER_MODEL = "accounts.CustomUser"
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

AUTHENTICATION_BACKENDS: list[str] = ["accounts.backends.EmailBackend"]

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS: list[Path] = [BASE_DIR / "static"]

# Media files
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Redis
REDIS_URL: str = config("REDIS_URL", cast=str)

# Cache
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }
}

# Celery
CELERY_BROKER_URL: str = REDIS_URL
CELERY_RESULT_BACKEND = "django-db"
CELERY_CACHE_BACKEND = "default"
CELERY_ACCEPT_CONTENT: list[str] = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ROUTES: dict[str, dict[str, str]] = {
    "projects.tasks.process_xml_upload": {"queue": "upload"},
    "projects.tasks.execute_test_run_case": {"queue": "execution"},
}

# Channels
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [REDIS_URL],
        },
    },
}

# Logging
LOG_LEVEL: str = config("LOG_LEVEL", default="INFO", cast=str)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "color": {
            "()": "colorlog.ColoredFormatter",
            "format": "%(log_color)s%(levelname)-8s%(reset)s %(blue)s%(name)s%(reset)s %(message)s",
            "log_colors": {
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "color",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "agents": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        "projects": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}

# Docker Model Runner
DMR_HOST: str = config("DMR_HOST", default="localhost", cast=str)
DMR_PORT: str = config("DMR_PORT", default="12434", cast=str)
DMR_MODEL: str = config("DMR_MODEL", default="ai/mistral", cast=str)
DMR_VISION_MODEL: str = config("DMR_VISION_MODEL", default="ai/qwen3-vl", cast=str)
DMR_TEMPERATURE: float = config("DMR_TEMPERATURE", default=0.1, cast=float)
DMR_MAX_TOKENS: int = config("DMR_MAX_TOKENS", default=4096, cast=int)
DMR_REQUEST_TIMEOUT: int = config("DMR_REQUEST_TIMEOUT", default=600, cast=int)

# OpenAI API (for vision model)
OPENAI_API_KEY: str = config("OPENAI_API_KEY", default="", cast=str)
OPENAI_BASE_URL: str = config(
    "OPENAI_BASE_URL",
    default="https://api.openai.com/v1/chat/completions",
    cast=str,
)
OPENAI_VISION_MODEL: str = config("OPENAI_VISION_MODEL", default="gpt-4o", cast=str)
OPENAI_TEMPERATURE: float = config("OPENAI_TEMPERATURE", default=0.1, cast=float)
OPENAI_MAX_TOKENS: int = config("OPENAI_MAX_TOKENS", default=4096, cast=int)
OPENAI_REQUEST_TIMEOUT: int = config("OPENAI_REQUEST_TIMEOUT", default=120, cast=int)

# Vision Backend: "dmr" or "openai"
VISION_BACKEND: str = config("VISION_BACKEND", default="dmr", cast=str)

# Agent
AGENT_MAX_ITERATIONS: int = config("AGENT_MAX_ITERATIONS", default=30, cast=int)
AGENT_TIMEOUT_SECONDS: int = config("AGENT_TIMEOUT_SECONDS", default=300, cast=int)

# SearXNG (metasearch engine)
SEARXNG_BASE_URL: str = config(
    "SEARXNG_BASE_URL", default="http://localhost:8888", cast=str
)
SEARXNG_MAX_RESULTS: int = config("SEARXNG_MAX_RESULTS", default=5, cast=int)
SEARXNG_REQUEST_TIMEOUT: int = config("SEARXNG_REQUEST_TIMEOUT", default=15, cast=int)

# Search page fetching (trafilatura)
SEARCH_FETCH_PAGE_COUNT: int = config("SEARCH_FETCH_PAGE_COUNT", default=3, cast=int)
SEARCH_PAGE_MAX_LENGTH: int = config("SEARCH_PAGE_MAX_LENGTH", default=2000, cast=int)
SEARCH_PAGE_FETCH_TIMEOUT: int = config(
    "SEARCH_PAGE_FETCH_TIMEOUT", default=10, cast=int
)

# Upload size limits
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB

# Controller
CONTROLLER_SERVER_HOST: str = config(
    "CONTROLLER_SERVER_HOST", default="localhost", cast=str
)
CONTROLLER_SERVER_PORT: int = config("CONTROLLER_SERVER_PORT", default=8000, cast=int)
CONTROLLER_AGENT_CONNECT_TIMEOUT: int = config(
    "CONTROLLER_AGENT_CONNECT_TIMEOUT", default=60, cast=int
)

# Output Summarizer
DMR_SUMMARIZER_MODEL: str = config(
    "DMR_SUMMARIZER_MODEL", default="ai/mistral", cast=str
)
OUTPUT_SUMMARIZE_THRESHOLD: int = config(
    "OUTPUT_SUMMARIZE_THRESHOLD", default=2000, cast=int
)
OUTPUT_SUMMARIZE_CHUNK_SIZE: int = config(
    "OUTPUT_SUMMARIZE_CHUNK_SIZE", default=6000, cast=int
)

# Context Summarizer
CONTEXT_SUMMARIZE_THRESHOLD: int = config(
    "CONTEXT_SUMMARIZE_THRESHOLD", default=20000, cast=int
)
CONTEXT_PRESERVE_LAST_MESSAGES: int = config(
    "CONTEXT_PRESERVE_LAST_MESSAGES", default=6, cast=int
)
CONTEXT_SUMMARIZE_CHUNK_SIZE: int = config(
    "CONTEXT_SUMMARIZE_CHUNK_SIZE", default=8000, cast=int
)

# OmniParser (client-side only — server runs as standalone FastAPI service)
OMNIPARSER_URL: str = config("OMNIPARSER_URL", default="", cast=str)
OMNIPARSER_API_KEY: str = config("OMNIPARSER_API_KEY", default="", cast=str)
OMNIPARSER_REQUEST_TIMEOUT: int = config(
    "OMNIPARSER_REQUEST_TIMEOUT", default=600, cast=int
)
