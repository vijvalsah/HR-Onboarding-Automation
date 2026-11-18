# Onboarding automation (Stage 1)

FastAPI backend scaffold with environment-based configuration and logging.

## Virtual environment (Windows / PowerShell)

From the project root (`Automate Interview`):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Copy environment template and adjust values:

```powershell
copy .env.example .env
```

## Run the API

```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open docs: `http://127.0.0.1:8000/docs`  
Health check: `http://127.0.0.1:8000/api/v1/health`

## Project layout (Stage 1)

- `app/main.py` — FastAPI app factory and ASGI entrypoint
- `app/core/config.py` — `pydantic-settings` from environment / `.env`
- `app/core/logging.py` — stdout logging, optional JSON for production
- `app/api/routes/` — HTTP routers (health only for now)

Later stages add database models, Celery, auth, and integrations under the same `app/` package.
