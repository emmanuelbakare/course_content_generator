# Course Content Generator

AI-assisted course authoring application built with Django 6.

## Local setup

From the repository root in PowerShell:

```powershell
.\venvi\Scripts\Activate.ps1
Copy-Item .env.example .env
pip install -r requirements\base.txt -r requirements\development.txt
cd src
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/ in a browser.

## Background worker

The application is wired for Celery and Redis. Start Redis, then use a second terminal from the repository root:

```powershell
.\venvi\Scripts\Activate.ps1
cd src
celery -A config worker --loglevel=info
```

Configure `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` in `.env` when Redis is not running on the default local address.

## Validation

```powershell
cd src
python manage.py check
python manage.py test
ruff check ..\src
mypy ..\src
```

Type checking is configured as a gradual migration: configuration and shared infrastructure are checked now, while the established Django domain modules are explicitly exempt until their model and view APIs are annotated.

## Deployment: Django, Celery, and Redis

Deploy the web application and Celery worker as separate processes against the same PostgreSQL database and Redis instance. Run migrations exactly once during each release; run `collectstatic` before starting the web process.

Required production environment values:

```text
DJANGO_DEBUG=False
DJANGO_SECRET_KEY=<long-random-secret>
DJANGO_ALLOWED_HOSTS=courses.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://courses.example.com
DATABASE_URL=postgresql://user:password@db-host:5432/course_generator
CELERY_BROKER_URL=redis://redis-host:6379/0
CELERY_RESULT_BACKEND=redis://redis-host:6379/1
DJANGO_SECURE_SSL_REDIRECT=True
DJANGO_USE_X_FORWARDED_PROTO=True
DJANGO_SESSION_COOKIE_SECURE=True
DJANGO_CSRF_COOKIE_SECURE=True
DJANGO_SECURE_HSTS_SECONDS=31536000
DJANGO_SECURE_HSTS_PRELOAD=True
```

Release and process commands (run from `src`):

```powershell
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py check --deploy
gunicorn config.wsgi:application --bind 0.0.0.0:8000
celery -A config worker --loglevel=info
```

Terminate TLS at a trusted reverse proxy and pass `X-Forwarded-Proto: https` only from that proxy. Private exports are served through authenticated Django download views; do not configure the web server to expose `MEDIA_ROOT`. Application and Celery logs are JSON and redact fields such as API keys, tokens, passwords, and authorization headers. Store all secrets in the platform secret manager, not in the repository or Django database.
