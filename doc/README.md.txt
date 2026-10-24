<div align="center">

# 🔀 RouteLLM — Intelligent Multi-Model AI Router

**A self-healing LLM gateway that routes tasks to the best-fit model and automatically fails over when one breaks.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black)](https://react.dev)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-316192?logo=postgresql&logoColor=white)](https://postgresql.org)
[![Redis](https://img.shields.io/badge/Redis-DC382D?logo=redis&logoColor=white)](https://redis.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[Live Demo](#) · [Video Walkthrough](#) · [Architecture](#-architecture) · [Report Bug](#)

![Dashboard Preview](./docs/assets/dashboard-preview.gif)

</div>

---

## 📌 Overview

RouteLLM is an intelligent middle-layer API that sits between your application and multiple LLM providers (GLM, Gemini, Groq, etc.). Instead of hardcoding which model to call, RouteLLM:

- **Classifies** each incoming task (code, creative writing, Q&A, summarization)
- **Scores** every available model based on capability fit, historical success rate, cost, and latency
- **Executes** the highest-scoring model, and **automatically falls back** through a ranked chain if that model fails, times out, runs out of credits, or returns bad output
- **Self-heals** using a circuit breaker pattern — temporarily disabling models that keep failing and re-testing them after a cooldown
- **Visualizes** everything in a real-time animated dashboard — live request feed, model health, fallback chains, cost analytics

> Built to solve a real production problem: apps that depend on a single LLM provider break the moment that provider rate-limits, goes down, or runs out of credits. RouteLLM makes multi-model usage resilient and observable.

---

## 🎥 Demo

| Live Routing Feed | Fallback Chain Visualization | Model Health |
|---|---|---|
| ![demo1](./docs/assets/demo-feed.gif) | ![demo2](./docs/assets/demo-fallback.gif) | ![demo3](./docs/assets/demo-health.gif) |

---

## ✨ Key Features

- 🧠 **Task-aware routing** — classifies prompts and matches them to the model best suited for that task type
- 📊 **Confidence scoring** — every routing decision shows a transparent confidence % based on historical performance
- 🔁 **Automatic fallback chain** — up to N models attempted in ranked order on any failure
- 🛡️ **Circuit breaker** — unhealthy models auto-disabled and auto-recovered (CLOSED → OPEN → HALF_OPEN)
- 💰 **Quota & credit tracking** — proactively skips models that have exhausted rate limits/credits before even calling them
- 📈 **Real-time analytics dashboard** — success rate, cost saved vs. single-model baseline, latency trends, model usage breakdown
- 🧪 **Fully tested** — unit + integration tests with mocked provider failures (no real API credits burned in CI)
- 🐳 **Dockerized** — single `docker-compose up` spins up the full stack

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  FRONTEND (React + Vite)                 │
│   Live feed · Model health · Fallback graph · Analytics  │
└───────────────────────┬───────────────────────────────────┘
                         │ REST
┌───────────────────────▼───────────────────────────────────┐
│                  BACKEND (FastAPI, async)                  │
│  Classifier → Scorer → Executor → Circuit Breaker          │
│                           │                                 │
│                 ┌─────────▼─────────┐                      │
│                 │  Provider Adapters │                      │
│                 │ GLM | Gemini | Groq│                      │
│                 └────────────────────┘                      │
└───────────────────────┬────────────────┬───────────────────┘
                         │                │
                  ┌──────▼──────┐  ┌──────▼──────┐
                  │ PostgreSQL  │  │    Redis     │
                  │ logs/stats  │  │ circuit/quota│
                  └─────────────┘  └──────────────┘
```

**Design patterns used:**
- **Strategy Pattern** — each LLM provider implements a common `ModelProvider` interface, fully swappable
- **Chain of Responsibility** — fallback execution tries models in ranked order until one succeeds
- **Circuit Breaker Pattern** — resilience engineering to isolate failing dependencies
- **Observer-style logging** — every routing decision emits a structured log event for the dashboard

📄 Full docs: [`/docs/PRD.md`](./docs/PRD.md) · [`/docs/TRD.md`](./docs/TRD.md) · [`/docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) · [`/docs/DESIGN.md`](./docs/DESIGN.md)

---

## 🧮 How Routing Works

```
Score(model, task) =
    0.35 × capability_match
  + 0.30 × historical_success_rate
  + 0.15 × (1 − normalized_cost)
  + 0.10 × (1 − normalized_latency)
  + 0.10 × availability   (0 if circuit-broken)

confidence_% = historical_success_rate(model, task_type) × capability_match
```

Models are ranked by score → the top-ranked model is called first → on failure (API error, timeout, rate limit, empty/garbage output), the next-ranked model is tried automatically, up to `max_fallbacks`.

---

## 🛠️ Tech Stack

**Backend:** Python · FastAPI · httpx (async) · SQLModel · PostgreSQL · Redis · Pydantic v2
**Frontend:** React (Vite) · TailwindCSS · Framer Motion · Recharts · Zustand
**Models integrated:** GLM-4-Flash (Zhipu AI) · Gemini 2.0 Flash (Google) · Groq (Llama)
**Testing:** Pytest, pytest-asyncio, httpx-mock · Vitest, React Testing Library
**DevOps:** Docker, docker-compose, GitHub Actions CI · deployed on Railway (backend) + Vercel (frontend)

---

## 📂 Project Structure

```
routellm/
├── backend/
│   ├── app/
│   │   ├── api/            # route handlers
│   │   ├── core/           # classifier, scorer, executor, circuit breaker
│   │   ├── providers/      # GLM, Gemini, Groq adapters
│   │   ├── models/         # SQLModel DB models
│   │   └── config.py
│   └── tests/
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── pages/
│   │   └── store/
│   └── tests/
├── docs/
│   ├── PRD.md
│   ├── TRD.md
│   ├── ARCHITECTURE.md
│   ├── DESIGN.md
│   └── assets/              # screenshots, gifs
├── docker-compose.yml
└── README.md
```

---

## 🚀 Getting Started

### Prerequisites
- Docker & Docker Compose
- API keys: GLM (Zhipu AI), Gemini, Groq — free tiers work fine

### 1. Clone & configure
```bash
git clone https://github.com/[your-username]/routellm.git
cd routellm
cp .env.example .env
# Add your API keys to .env
```

### 2. Run with Docker
```bash
docker-compose up --build
```
- Backend → `http://localhost:8000` (docs at `/docs`)
- Frontend → `http://localhost:5173`

### 3. Run locally without Docker
```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload

# Frontend
cd frontend
npm install
npm run dev
```

### 4. Run tests
```bash
# Backend
cd backend && pytest -v

# Frontend
cd frontend && npm run test
```

---

## 📡 API Reference

### `POST /api/v1/route`
```json
// Request
{
  "prompt": "Write a Python function to reverse a linked list",
  "max_fallbacks": 3
}

// Response
{
  "result": "...",
  "model_used": "glm-4-flash",
  "confidence_score": 0.91,
  "fallback_chain_attempted": [],
  "latency_ms": 640,
  "tokens_used": 210
}
```

| Endpoint | Method | Description |
|---|---|---|
| `/api/v1/route` | POST | Main routing endpoint |
| `/api/v1/models` | GET | Model registry + live health status |
| `/api/v1/logs` | GET | Paginated routing decision history |
| `/api/v1/analytics` | GET | Aggregate success rate, cost saved, usage breakdown |
| `/api/v1/health` | GET | Service health check |

Full interactive API docs available at `/docs` (Swagger UI) once running.

---

## 📊 Results / Metrics

> Fill in with real numbers after testing — this section is what recruiters actually read.

- ✅ **[X]%** request success rate including fallback recovery (vs. [Y]% on a single-model baseline)
- ⚡ Routing overhead: **~[X]ms** (excluding model inference time)
- 💰 **[X]%** cost reduction by routing cheaper/faster models for simple tasks
- 🔁 Successfully recovered from simulated provider outages in **100%** of fallback test cases

---

## 🧪 Testing

- **Unit tests** — scoring formula, task classifier, circuit breaker state transitions
- **Integration tests** — full routing flow with mocked provider failures (429, timeout, garbage output)
- **Frontend tests** — component rendering + live feed animation behavior
- CI runs the full suite on every push via GitHub Actions

See [`/docs/TESTING.md`](./docs/TESTING.md) for the full test case matrix.

---

## 🗺️ Roadmap

- [x] Rule-based task classification
- [x] Scoring engine + confidence calculation
- [x] Circuit breaker + quota tracking
- [x] Real-time animated dashboard
- [ ] Embedding-based task classification (upgrade from rule-based)
- [ ] Adaptive routing that learns from live feedback over time
- [ ] Streaming response support
- [ ] Multi-tenant API key management

---

## 🤔 Why I Built This

[2-3 sentences, written by you personally — e.g.: "I kept running into rate limits and outages while building AI side projects, and realized there was no simple way to make multi-LLM apps resilient. This project explores production patterns like circuit breakers and adaptive routing — the same resilience engineering used by real LLM gateway products like OpenRouter and Portkey — applied to a system I designed and built end-to-end."]

---

## 📄 License

MIT © [Your Name]

---

<div align="center">

**[⬆ back to top](#-routellm--intelligent-multi-model-ai-router)**

</div>