# DukaFlow

A B2B commerce engine built with FastAPI, designed for WhatsApp ordering, merchant operations, catalog management, and automated M-Pesa payment processing.

---

## Tech Stack

- **Framework:** FastAPI (Python 3.12+)
- **Database:** PostgreSQL (SQLAlchemy async + Alembic migrations)
- **Cache / Queues:** Redis
- **Integrations:** Daraja (M-Pesa Express / STK Push), WhatsApp Cloud API

---

## Quickstart

### 1. Clone & Set Up Environment

```bash
git clone <YOUR_REPO_URL>
cd Dukaflow/app

# Create and activate virtual environment
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Copy the example environment file and update secrets as needed:

```bash
cp .env.example .env
```

### 3. Start Database & Redis

```bash
docker compose up -d
```

### 4. Run Migrations

```bash
alembic upgrade head
```

### 5. Start the Server

```bash
uvicorn main:app --reload
```

Interactive API docs will be available at: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

---

## Core Features

- **Merchants & Auth:** JWT authentication, onboarding, and merchant store management.
- **Products & Inventory:** Product catalog management and stock tracking.
- **Orders & Checkout:** Order lifecycle with automatic stale-order sweeping.
- **M-Pesa Integration:** STK Push initiate & callback webhooks for automated payment reconciliation.
- **WhatsApp Webhook:** Interactive messaging and bot order processing.

---

## Testing & Linting

```bash
pytest
ruff check .
mypy .
```
