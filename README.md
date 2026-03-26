# 🌾 Agri Advisory System — Punjab, Pakistan

An AI-powered agricultural advisory platform for smallholder farmers in Punjab. Farmers send WhatsApp photos of their crops and receive disease diagnoses and treatment advice in Urdu within 30 seconds. Extension workers monitor field health across the district from a React dashboard.

---

## Table of Contents

- [System Overview](#system-overview)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Project Structure](#project-structure)
- [Local Setup](#local-setup)
  - [1. Clone the Repository](#1-clone-the-repository)
  - [2. Backend Setup](#2-backend-setup)
  - [3. Database Setup](#3-database-setup)
  - [4. Run Celery Workers](#4-run-celery-workers)
  - [5. Frontend — Agri Dashboard](#5-frontend--agri-dashboard)
  - [6. Frontend — Farmer Portal](#6-frontend--farmer-portal)
- [Environment Variables](#environment-variables)
- [API Reference](#api-reference)
- [WhatsApp Webhook Setup](#whatsapp-webhook-setup)
- [How the AI Pipeline Works](#how-the-ai-pipeline-works)
- [Build Phases](#build-phases)

---

## System Overview

The system has four independent processing tracks:

| Track | Description | Latency |
|---|---|---|
| **WhatsApp photo inference** | Farmer sends image → disease diagnosis → reply | < 30s |
| **Async advisory generation** | LangGraph + Gemini 2.5 Flash pipeline | 10–30s |
| **Scheduled batch jobs** | Satellite pulls, weather refresh, weekly reports | Nightly / Weekly |
| **REST API** | Serves the React dashboard for extension workers | < 200ms |

---

## Architecture

```
WhatsApp Cloud API
       │
       ▼
FastAPI Webhook Handler
       │
       ├─── Onboarding State Machine (Redis sessions)
       │
       └─── Celery Task Queue (Redis broker)
                   │
                   ▼
         LangGraph Advisory Pipeline
         ┌─────────────────────────────┐
         │ fetch_image_bytes           │
         │ analyze_image_for_disease   │  ← Gemini Vision
         │ extract_disease_hint        │
         │ generate_advisory           │  ← Gemini 2.5 Flash
         │ finalize_response           │
         └─────────────────────────────┘
                   │
                   ▼
          PostgreSQL (advisory_requests)
                   │
                   ▼
          WhatsApp reply to farmer

Celery Beat (Scheduled)
  ├── Nightly: update_field_health_snapshots (Open-Meteo weather + NDVI estimation)
  └── Weekly:  generate_weekly_farmer_reports (Sunday 8am PKT)

React Frontends
  ├── agri-dashboard (port 3000) — Extension workers
  └── farmer-portal  (port 3001) — Farmer registration + disease scan
```

---

## Tech Stack

**Backend**
- [FastAPI](https://fastapi.tiangolo.com/) — async REST API
- [SQLAlchemy](https://www.sqlalchemy.org/) (async) + [PostgreSQL](https://www.postgresql.org/) with PostGIS
- [Alembic](https://alembic.sqlalchemy.org/) — database migrations
- [Celery](https://docs.celeryq.dev/) + [Redis](https://redis.io/) — task queue and beat scheduler
- [LangGraph](https://langchain-ai.github.io/langgraph/) — AI advisory pipeline
- [Google Gemini 2.5 Flash](https://ai.google.dev/) — vision + text LLM
- [Open-Meteo](https://open-meteo.com/) — free weather API (no key required)

**Frontend**
- [React 19](https://react.dev/) + [Vite 8](https://vite.dev/)
- [Tailwind CSS v4](https://tailwindcss.com/)
- [React Leaflet](https://react-leaflet.js.org/) — interactive field maps
- [Axios](https://axios-http.com/)

---

## Prerequisites

Make sure you have the following installed:

- **Python 3.11+**
- **Node.js 20+** and **npm**
- **PostgreSQL 16** with the **PostGIS extension**
- **Redis 7**
- A **Google AI Studio** API key (for Gemini)
- *(Optional)* A **WhatsApp Business Cloud API** token and phone number ID for live WhatsApp messaging

---

## Project Structure

```
agri_advisory/
├── app/
│   ├── main.py                    # FastAPI app factory
│   ├── api/v1/
│   │   ├── auth.py                # JWT register/login
│   │   ├── farmers.py             # Farmer CRUD
│   │   ├── fields.py              # Field registration
│   │   ├── dashboard.py           # Stats, maps, farmer detail
│   │   ├── detections.py          # Direct image upload scan
│   │   └── webhooks.py            # WhatsApp webhook receiver
│   ├── core/
│   │   ├── config.py              # Pydantic settings
│   │   ├── security.py            # JWT + bcrypt
│   │   └── dependencies.py        # DB session, auth, Redis
│   ├── services/
│   │   ├── advisory_agent.py      # LangGraph pipeline
│   │   ├── satellite.py           # Open-Meteo + NDVI estimation
│   │   ├── whatsapp.py            # WhatsApp Cloud API client
│   │   └── onboarding.py          # WhatsApp onboarding state machine
│   ├── tasks/
│   │   ├── celery_app.py          # Celery + Beat config
│   │   ├── advisory_tasks.py      # Async advisory generation
│   │   ├── satellite_tasks.py     # Nightly field health updates
│   │   └── report_tasks.py        # Weekly farmer summaries
│   ├── models/                    # SQLAlchemy ORM models
│   └── schemas/                   # Pydantic request/response schemas
├── agri-dashboard/                # React dashboard (extension workers)
├── farmer-portal/                 # React portal (farmer registration + scan)
├── alembic/                       # DB migrations
├── requirements.txt
└── .env                           # ← you create this
```

---

## Local Setup

### 1. Clone the Repository

```bash
git clone https://github.com/your-username/agri-advisory.git
cd agri-advisory
```

---

### 2. Backend Setup

**Create and activate a virtual environment:**

```bash
python -m venv venv
source venv/bin/activate        # Linux/macOS
# venv\Scripts\activate         # Windows
```

**Install Python dependencies:**

```bash
pip install fastapi uvicorn[standard] sqlalchemy[asyncio] asyncpg psycopg2-binary \
  alembic pydantic-settings passlib[bcrypt] python-jose[cryptography] \
  celery redis langchain langgraph langchain-google-genai \
  openmeteo-requests requests-cache retry-requests httpx python-dotenv
```

> Or consolidate everything into a `requirements.txt` and run `pip install -r requirements.txt`.

**Create your `.env` file** (see [Environment Variables](#environment-variables) below).

**Start the FastAPI server:**

```bash
uvicorn app.main:app --reload --port 8000
```

The API will be available at `http://localhost:8000`. Interactive docs are at `http://localhost:8000/docs`.

---

### 3. Database Setup

**Create the PostgreSQL database:**

```bash
psql -U postgres
CREATE DATABASE agri_advisory;
\c agri_advisory
CREATE EXTENSION postgis;
\q
```

**Run all Alembic migrations:**

```bash
alembic upgrade head
```

**Create your first admin/extension worker user** via the API or `psql`:

```bash
# Via the API (recommended)
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "admin@agri.pk",
    "password": "yourpassword",
    "role": "extension_worker"
  }'
```

---

### 4. Run Celery Workers

Open two additional terminal tabs (with the venv activated):

**Tab 1 — Advisory + default worker:**

```bash
celery -A app.tasks.celery_app worker --loglevel=info -Q advisory,default --pool=solo
```

**Tab 2 — Celery Beat scheduler** (for nightly/weekly jobs):

```bash
celery -A app.tasks.celery_app beat --loglevel=info
```

> **Note:** `--pool=solo` is recommended for local development on Windows. On Linux/macOS you can use the default prefork pool.

---

### 5. Frontend — Agri Dashboard

The dashboard is for extension workers (login required).

```bash
cd agri-dashboard
npm install
npm run dev
```

Open `http://localhost:3000`. Log in with the admin user you created above.

---

### 6. Frontend — Farmer Portal

The portal is for farmer self-registration and crop disease scanning (no login required).

```bash
cd farmer-portal
npm install
npm run dev
```

Open `http://localhost:3001`.

---

## Environment Variables

Create a `.env` file in the project root:

```env
# ── Database ──────────────────────────────────────────────────
DATABASE_URL=postgresql+asyncpg://postgres:yourpassword@localhost:5432/agri_advisory

# ── Security ──────────────────────────────────────────────────
SECRET_KEY=your-very-long-random-secret-key-here

# ── Google Gemini (required for AI advisory) ──────────────────
GOOGLE_API_KEY=your-google-ai-studio-api-key

# ── Redis ─────────────────────────────────────────────────────
REDIS_URL=redis://localhost:6379/0

# ── WhatsApp Cloud API (optional — needed for live WhatsApp) ──
WHATSAPP_TOKEN=your-whatsapp-access-token
WHATSAPP_PHONE_NUMBER_ID=your-phone-number-id
WHATSAPP_VERIFY_TOKEN=agri_advisory_webhook_secret

# ── Debug ─────────────────────────────────────────────────────
DEBUG=False
```

**Getting your Google API key:**
1. Go to [Google AI Studio](https://aistudio.google.com/)
2. Click **Get API Key** → **Create API Key**
3. Copy the key into `GOOGLE_API_KEY`

**Getting WhatsApp credentials** *(optional for local dev)*:
1. Create a Meta Developer app at [developers.facebook.com](https://developers.facebook.com/)
2. Add the **WhatsApp** product
3. Copy the temporary access token → `WHATSAPP_TOKEN`
4. Copy the Phone Number ID → `WHATSAPP_PHONE_NUMBER_ID`

> Without WhatsApp credentials, the advisory pipeline still works fully — it just won't send replies back to WhatsApp. You can test everything through the Farmer Portal's **Disease Scan** tab and the dashboard.

---

## API Reference

All routes are prefixed with `/api/v1`. Interactive docs: `http://localhost:8000/docs`

| Method | Endpoint | Auth | Description |
|---|---|---|---|
| `POST` | `/auth/register` | Public | Register a new user |
| `POST` | `/auth/login` | Public | Login, get JWT token |
| `GET` | `/auth/me` | JWT | Get current user |
| `POST` | `/farmers/` | Public | Register a farmer |
| `GET` | `/farmers/{phone}` | Public | Get farmer by phone number |
| `DELETE` | `/farmers/{phone}` | Extension Worker | Deactivate a farmer |
| `POST` | `/fields/` | Public | Register a field |
| `GET` | `/fields/farmer/{phone}` | Public | List fields for a farmer |
| `GET` | `/dashboard/stats` | Extension Worker | Summary stat cards |
| `GET` | `/dashboard/farmers` | Extension Worker | All farmers with health |
| `GET` | `/dashboard/farmers/{phone}` | Extension Worker | Farmer detail + advisories |
| `GET` | `/dashboard/field-health` | Extension Worker | Map data for all fields |
| `GET` | `/dashboard/advisories/recent` | Extension Worker | Recent advisory feed |
| `POST` | `/detections/scan` | Public | Upload crop image for analysis |
| `GET` | `/detections/scan/{id}` | Public | Poll scan result |
| `POST` | `/webhooks/webhook` | WhatsApp | Incoming WhatsApp messages |
| `GET` | `/webhooks/webhook` | WhatsApp | Webhook verification |

**Example: submit a disease scan via curl**

```bash
curl -X POST http://localhost:8000/api/v1/detections/scan \
  -F "farmer_phone=+923001234567" \
  -F "image=@/path/to/crop_photo.jpg" \
  -F "query_text=پتے پیلے ہو رہے ہیں"
```

**Example: poll for the result**

```bash
curl http://localhost:8000/api/v1/detections/scan/<advisory_id>
```

**Example: simulate a WhatsApp message locally**

```bash
curl -X POST http://localhost:8000/api/v1/webhooks/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "message_id": "test-msg-001",
    "from": "+923001234567",
    "text": "میری گندم کے پتے پیلے ہو رہے ہیں",
    "image_url": null
  }'
```

---

## WhatsApp Webhook Setup

To receive live WhatsApp messages, your server must be publicly accessible. For local development, use [ngrok](https://ngrok.com/):

```bash
ngrok http 8000
```

Then in the Meta Developer Console:
1. Go to your app → **WhatsApp** → **Configuration**
2. Set the Webhook URL to: `https://<your-ngrok-id>.ngrok.io/api/v1/webhooks/webhook`
3. Set the Verify Token to the value of `WHATSAPP_VERIFY_TOKEN` in your `.env`
4. Subscribe to the `messages` webhook field

---

## How the AI Pipeline Works

When a farmer sends a WhatsApp message or uploads a photo through the portal, the request goes through a **LangGraph state machine**:

```
fetch_image_bytes
       │
       ▼
analyze_image_for_disease    ← Gemini Vision JSON diagnosis
       │
       ▼
extract_disease_hint         ← Falls back to keyword matching if no image
       │
       ▼
generate_advisory            ← Gemini generates bilingual Urdu/English advice
       │
    ┌──┴──┐
    │     │
fallback  finalize_response
```

The advisory prompt injects structured farmer context (crop type, field size, soil, NDVI score, current weather) so responses are specific enough to act on — product names, dosages, timings — not generic advice.

---

## Build Phases

The project follows a phased approach:

| Phase | Focus | Status |
|---|---|---|
| **Phase 1** | End-to-end: WhatsApp → Celery → inference → reply | ✅ Complete |
| **Phase 2** | LangGraph advisory agent, RAG knowledge base, React dashboard | ✅ Complete |
| **Phase 3** | Satellite NDVI pipeline, Celery Beat, weather integration | ✅ Complete |
| **Phase 4** | Sentry, Prometheus metrics, structured logging, HTTPS, rate limiting | 🔜 Ongoing |

---

## Contributing

Pull requests are welcome. For major changes, please open an issue first.