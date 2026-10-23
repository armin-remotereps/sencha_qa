import django_stubs_ext
from dotenv import load_dotenv

django_stubs_ext.monkeypatch()

from pathlib import Path

from decouple import Choices, Csv, config

from auto_tester.encryption_key import validate_fernet_key

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Security
SECRET_KEY: str = config("SECRET_KEY", cast=str)
# Fernet key (urlsafe base64, 32 bytes) used to encrypt per-project secrets
# such as TestRail API keys at rest. Generate one with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FIELD_ENCRYPTION_KEY: str = validate_fernet_key(
    config("FIELD_ENCRYPTION_KEY", cast=str)
)
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
    "machine_setup",
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
                "auto_tester.context_processors.branding",
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
# With acks_late, redis redelivers a message still unacked after this many
# seconds (default 3600) even while a worker is running it. Keep it well above
# the longest task limit (projects.checked_push_limits.CHECKED_PUSH_TIME_LIMIT,
# 3660s; machine_setup.turn_state.SETUP_TURN_TIME_LIMIT, timeout + 360s) so a
# long "Send checked to TestRail" or setup turn is never run twice.
CELERY_BROKER_TRANSPORT_OPTIONS: dict[str, int] = {"visibility_timeout": 4 * 3600}
CELERY_TASK_ROUTES: dict[str, dict[str, str]] = {
    "projects.tasks.execute_test_run_case": {"queue": "execution"},
    "projects.tasks.refine_project_prompt_task": {"queue": "upload"},
    "projects.tasks.import_testrail_cases": {"queue": "upload"},
    "projects.tasks.import_testrail_run_cases": {"queue": "upload"},
    "projects.tasks.push_test_run_results": {"queue": "upload"},
    "projects.tasks.push_checked_results_to_testrail": {"queue": "upload"},
    "machine_setup.tasks.run_setup_turn": {"queue": "execution"},
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

# OpenAI API (shared credentials for all OpenAI-routed calls)
OPENAI_API_KEY: str = config("OPENAI_API_KEY", default="", cast=str)
# The full Responses API endpoint. Chat Completions is not supported: it rejects
# function tools while gpt-5.6-sol is reasoning.
OPENAI_BASE_URL: str = config(
    "OPENAI_BASE_URL",
    default="https://api.openai.com/v1/responses",
    cast=str,
)
# Every LLM role — agent, orchestrator, sub-agent, summarizer, refiner and
# vision — runs on this one model. Output ceilings live beside the request
# builder in agents/services/llm_config.py, and `temperature` is never sent at
# all: the gpt-5 family rejects any value other than its own default.
OPENAI_MODEL: str = config("OPENAI_MODEL", default="gpt-5.6-sol", cast=str)
# A reasoning model spends unbounded hidden thinking time before the first
# visible token and may then emit up to MAX_OUTPUT_TOKENS, so the per-request
# timeout is deliberately generous.
OPENAI_REQUEST_TIMEOUT: int = config("OPENAI_REQUEST_TIMEOUT", default=900, cast=int)
# Reasoning summary shown as [Thinking] in the agent debug log: auto, concise or
# detailed. Needs a verified OpenAI organisation (otherwise every call returns
# 400); leave blank to not request one. Reasoning effort is never sent, so the
# model's default applies.
OPENAI_REASONING_SUMMARY: str = config(
    "OPENAI_REASONING_SUMMARY",
    default="auto",
    cast=Choices(["", "auto", "concise", "detailed"]),
)

# Agent
AGENT_MAX_ITERATIONS: int = config("AGENT_MAX_ITERATIONS", default=300, cast=int)
AGENT_TIMEOUT_SECONDS: int = config("AGENT_TIMEOUT_SECONDS", default=300, cast=int)
# Longest tool result (chars) handed to the model; longer ones keep their head
# and tail. Generous on purpose: the model reads ~922k input tokens, and a
# Responses request sets truncation "auto" for anything beyond that.
AGENT_TOOL_RESULT_MAX_CHARS: int = config(
    "AGENT_TOOL_RESULT_MAX_CHARS", default=100_000, cast=int
)

# Machine setup (S0) chat agent: limits per user message (one agent turn).
MACHINE_SETUP_MAX_ITERATIONS: int = config(
    "MACHINE_SETUP_MAX_ITERATIONS", default=200, cast=int
)
MACHINE_SETUP_TURN_TIMEOUT_SECONDS: int = config(
    "MACHINE_SETUP_TURN_TIMEOUT_SECONDS", default=3600, cast=int
)

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
# How long an active test run waits for the Test Runner to reconnect after a
# drop. A test case may wait at most twice this in total; the wait does not
# count against agent time.
CONTROLLER_RECONNECT_GRACE_SECONDS: int = config(
    "CONTROLLER_RECONNECT_GRACE_SECONDS", default=300, cast=int
)
INTERACTIVE_CMD_TIMEOUT_SECONDS: int = config(
    "INTERACTIVE_CMD_TIMEOUT_SECONDS", default=300, cast=int
)
CLEANUP_TIMEOUT_SECONDS: int = config("CLEANUP_TIMEOUT_SECONDS", default=30, cast=int)
CONTROLLER_FIND_ELEMENT_TIMEOUT: int = config(
    "CONTROLLER_FIND_ELEMENT_TIMEOUT", default=120, cast=int
)
CONTROLLER_OMNIPARSER_READY_TIMEOUT: int = config(
    "CONTROLLER_OMNIPARSER_READY_TIMEOUT", default=600, cast=int
)

# Orchestrator (sub-agent architecture)
ORCHESTRATOR_MAX_SUBTASKS: int = config(
    "ORCHESTRATOR_MAX_SUBTASKS", default=30, cast=int
)
ORCHESTRATOR_MAX_RECOVERY_ATTEMPTS: int = config(
    "ORCHESTRATOR_MAX_RECOVERY_ATTEMPTS", default=1, cast=int
)

# Sub-Agent
SUB_AGENT_MAX_ITERATIONS: int = config(
    "SUB_AGENT_MAX_ITERATIONS", default=300, cast=int
)
SUB_AGENT_TIMEOUT_SECONDS: int = config(
    "SUB_AGENT_TIMEOUT_SECONDS", default=180, cast=int
)
SUB_AGENT_MIN_TIMEOUT_SECONDS: int = config(
    "SUB_AGENT_MIN_TIMEOUT_SECONDS", default=150, cast=int
)
SUB_AGENT_MAX_TIMEOUT_SECONDS: int = config(
    "SUB_AGENT_MAX_TIMEOUT_SECONDS", default=900, cast=int
)
