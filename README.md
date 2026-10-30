# Bhojan Backend

FastAPI backend for the Bhojan app ([`bhojan-android`](https://github.com/marava-tech/bhojan-android)). Serves at `https://bhojan-backend.marava.tech` (old host `gympulse-backend.marava.tech` is kept as an alias). Container `bhojan-backend`, port 8850. Interactive docs at `/docs`, health check at `/health`.

Some internal names still say GymPulse / Fitness OS (logger, OpenAPI title, `gympulse` database); they are legacy and not user-facing.

## What it does

- Email OTP auth (JWT), profile and account deletion, units preference (kg/lb, cm/ft-in).
- Food logging: AI photo scan, text describe, barcode lookup (OpenFoodFacts), saved foods and meals, daily totals, streaks, logging score.
- Supplements and daily check-ins, sleep, weight and body photos, body analysis, adaptive TDEE, weekly insights.
- AI quota: free vs premium daily scan caps, rewarded-ad extra scans, `/api/ai/quota`.
- Billing: RevenueCat webhook sets premium entitlement.
- FCM push, plus scheduled jobs: weekly insights (Sun 14:30 UTC) and end-of-day meal nudge (15:00 UTC).
- Gym session endpoints still exist but the app no longer uses them.

## Stack

FastAPI, Uvicorn, MongoDB (Motor), Pydantic, APScheduler, httpx. AI calls go through OpenRouter (Gemini models by default). Images are stored through the MinIO upload API.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # fill in real values
uvicorn main:app --reload --port 8850
```

## Environment

Required at startup: `MONGODB_URI`, `JWT_SECRET`, `FIREBASE_PROJECT_ID`.

| Group | Variables |
|---|---|
| Auth | `JWT_SECRET`, `JWT_EXPIRATION` |
| AI | `OPENROUTER_API_KEY`, `OPENROUTER_VISION_MODEL`, `OPENROUTER_TEXT_MODEL` |
| Quota | `AI_FREE_SCANS_PER_DAY` (5), `AI_PREMIUM_SCANS_PER_DAY` (15), `AI_MAX_REWARDS_PER_DAY` (5), `PREMIUM_TRIAL_DAYS` (3) |
| Billing | `REVENUECAT_WEBHOOK_AUTH`, `REVENUECAT_ENTITLEMENT` |
| Push | `FIREBASE_PROJECT_ID`, `FIREBASE_CREDENTIALS_PATH` |
| Storage | `UPLOAD_URL`, `UPLOAD_API_KEY` |
| Email | `EMAIL_ENABLED`, `EMAIL_FROM`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USERNAME`, `EMAIL_PASSWORD` |

`.env.example` lists the basics; real values live only in `/root/server/.env` on the VPS.

## Layout

```text
main.py       # app, router wiring, scheduler
auth.py       # JWT helpers
database.py   # Mongo connection and indexes
routers/      # API routes
models/       # Pydantic models
services/     # Gemini/OpenRouter, quota, entitlements, FCM, email, MinIO, OpenFoodFacts, TDEE
scripts/      # check_*.py sanity scripts, eval_accuracy.py, migrations
docs/         # BACKEND_CHANGES.md (v3 plan)
```

## Verify

```bash
python3 -m compileall .
python3 scripts/check_ai_quota.py   # and the other scripts/check_*.py
```

## Deploy

Merge to `main` triggers `.github/workflows/deploy.yml`: build image `ghcr.io/marava-tech/bhojan-backend:latest`, SSH to the VPS, recreate the container from `services/bhojan-backend/` in the `server` repo. Do not deploy by hand.

## Rules

- Never commit `.env*`, Firebase credentials, API keys, or DB credentials.
- Keep response changes in sync with the Android app.
- Do not log health, body, or nutrition request data.
