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

Open http://127.0.0.1:8000/ in a browser. The project currently exposes only Django's admin route at `/admin/`.

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
```
