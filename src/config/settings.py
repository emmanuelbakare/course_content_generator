"""Django settings for the Course Content Generator."""

import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent

load_dotenv(PROJECT_ROOT / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    """Read a conventional boolean environment variable."""
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    """Read a positive integer environment setting with a safe default."""
    return int(os.getenv(name, str(default)))


def database_config():
    """Use SQLite locally, or a standard PostgreSQL DATABASE_URL in deployment."""
    database_url = os.getenv('DATABASE_URL', '').strip()
    if not database_url:
        return {'ENGINE': 'django.db.backends.sqlite3', 'NAME': BASE_DIR / 'db.sqlite3'}
    parsed = urlparse(database_url)
    if parsed.scheme not in {'postgres', 'postgresql'}:
        raise RuntimeError('DATABASE_URL must use a PostgreSQL URL.')
    return {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': parsed.path.lstrip('/'),
        'USER': parsed.username or '',
        'PASSWORD': parsed.password or '',
        'HOST': parsed.hostname or '',
        'PORT': parsed.port or '',
        'CONN_MAX_AGE': env_int('DJANGO_DB_CONN_MAX_AGE', 60),
    }


DEBUG = env_bool("DJANGO_DEBUG", default=True)
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("DJANGO_SECRET_KEY must be configured when DJANGO_DEBUG is false.")
    SECRET_KEY = "django-insecure-development-only-change-me"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_htmx',
    'accounts',
    'courses',
    'generation',
    'exports',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django_htmx.middleware.HtmxMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


DATABASES = {
    'default': database_config(),
}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = os.getenv('DJANGO_TIME_ZONE', 'UTC')

USE_I18N = True

USE_TZ = True


STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://127.0.0.1:6379/0')
CELERY_RESULT_BACKEND = os.getenv('CELERY_RESULT_BACKEND', 'redis://127.0.0.1:6379/1')
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = int(os.getenv('CELERY_TASK_TIME_LIMIT', '900'))
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv('CELERY_TASK_SOFT_TIME_LIMIT', '840'))
CELERY_TASK_ALWAYS_EAGER = env_bool('CELERY_TASK_ALWAYS_EAGER', default=False)
CELERY_TASK_EAGER_PROPAGATES = env_bool('CELERY_TASK_EAGER_PROPAGATES', default=False)

# These remain development-friendly by default and become strict when DEBUG is false.
SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL_REDIRECT', default=not DEBUG)
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if env_bool(
    'DJANGO_USE_X_FORWARDED_PROTO', default=not DEBUG
) else None
SECURE_HSTS_SECONDS = env_int('DJANGO_SECURE_HSTS_SECONDS', 31_536_000 if not DEBUG else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool(
    'DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS', default=not DEBUG
)
SECURE_HSTS_PRELOAD = env_bool('DJANGO_SECURE_HSTS_PRELOAD', default=False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
SESSION_COOKIE_SECURE = env_bool('DJANGO_SESSION_COOKIE_SECURE', default=not DEBUG)
CSRF_COOKIE_SECURE = env_bool('DJANGO_CSRF_COOKIE_SECURE', default=not DEBUG)
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',')
    if origin.strip()
]

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {'redact': {'()': 'config.logging.RedactingFilter'}},
    'formatters': {'json': {'()': 'config.logging.JsonFormatter'}},
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'filters': ['redact'], 'formatter': 'json'}
    },
    'root': {'handlers': ['console'], 'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO')},
    'loggers': {
        'django.server': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'celery': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}

LOGIN_URL = 'accounts:login'
LOGIN_REDIRECT_URL = 'accounts:dashboard'
LOGOUT_REDIRECT_URL = 'accounts:login'
