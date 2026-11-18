# Automate Interview — Complete End-to-End Pipeline & Architecture

> AI-driven hiring platform: job posting → resume screening (AI suitability %) →
> automated availability phone call → multi-round interviews on Teams → dual
> (human + AI) feedback → AI suitability prediction → HR decisions → PDF offer →
> onboarding.

This document describes **every feature** with its **full end-to-end pipeline**,
**production-readiness**, **test cases**, **failure modes (why they fail + how
they're resolved)**, and the **cloud / Redis / queue architecture**.

For the outside-world integrations (phone, email, Teams, transcription, real
LLM), each is written as a **complete production pipeline as if implemented** —
the exact provider, data flow, config, cloud services, retries, and failure
handling — even where a local **mock adapter** runs today. Those are clearly
tagged so you always know what is live vs. mock.

---

## Table of contents

1. [System architecture](#1-system-architecture)
2. [Cloud, Redis, queues & async jobs](#2-cloud-redis-queues--async-jobs)
3. [Roles & access model](#3-roles--access-model)
4. [Data model](#4-data-model)
5. [Master hiring pipeline](#5-master-hiring-pipeline)
6. [Feature pipelines (implemented)](#6-feature-pipelines-implemented)
7. [External integrations — production pipelines](#7-external-integrations--production-pipelines)
8. [Core feature pipelines — deep dive](#8-core-feature-pipelines--deep-dive)
9. [Cross-cutting concerns](#9-cross-cutting-concerns)
10. [Failure modes & resolutions (catalog)](#10-failure-modes--resolutions-catalog)
11. [Test strategy & cases](#11-test-strategy--cases)
12. [Production readiness checklist](#12-production-readiness-checklist)
13. [Configuration reference](#13-configuration-reference)
14. [Status glossary & implementation matrix](#14-status-glossary--implementation-matrix)

---

## 1. System architecture

Two apps + a database, with a background worker tier for slow/external work.

```
┌───────────────┐   same-origin    ┌────────────────────┐         ┌──────────────┐
│ Browser (SPA) │ ─ /api/backend ─▶│ Next.js BFF proxy  │ ──JWT──▶ │  FastAPI     │
│ React 19      │ ◀─ httpOnly ───  │ cookie⇄Bearer      │         │  (async)     │
│ Next.js 15    │      cookie      │ middleware verifies│         │              │
└───────────────┘                  └────────────────────┘         └──────┬───────┘
                                                                          │
                          enqueue jobs (Celery) ─────────────────────────┤
                                                                          │
                 ┌───────────────┐     ┌───────────────┐          ┌───────▼───────┐
                 │  Redis        │◀───▶│ Celery workers│          │ SQLAlchemy    │
                 │  broker+cache │     │ (calls, email,│          │ 2.0 async     │
                 │  rate-limit   │     │  transcribe,  │          └───────┬───────┘
                 └───────────────┘     │  LLM scoring) │                  │
                                       └───────┬───────┘        ┌─────────▼─────────┐
                          Twilio / MS Graph /  │                │ SQLite (dev)      │
                          SMTP / OpenAI ◀───────┘               │ PostgreSQL (prod) │
                                                                └───────────────────┘
                                            Alembic migrations own the schema
```

**Key decisions**

- **BFF (Backend-for-Frontend):** the browser never holds the JWT. Login stores
  it in an **httpOnly cookie**; the Next.js proxy attaches it as a `Bearer` token
  to FastAPI. Middleware verifies JWT on every page route. XSS cannot read the token.
- **Async I/O end to end:** FastAPI + SQLAlchemy async engine; DB never blocks
  the event loop.
- **Provider pattern for everything external:** chat, assessment, email, Teams,
  call bot, transcription are **interfaces with swappable implementations**. A
  `mock` runs locally with zero credentials; a real provider is chosen by config.
- **Alembic owns the schema:** `alembic upgrade head` runs on startup; a fresh DB
  is built from migrations and existing ones evolve safely.
- **Worker tier (Celery + Redis):** anything slow or external (phone calls, email,
  transcription, LLM scoring) runs off the request path as a background job.

**Tech stack**

| Layer      | Technology                                                     |
|------------|----------------------------------------------------------------|
| Backend    | FastAPI, SQLAlchemy 2.0 async, Alembic, Pydantic v2            |
| Auth       | JWT (python-jose, HS256), bcrypt hashing                       |
| Docs       | reportlab (PDF offer letters)                                 |
| Workers    | Celery + Redis (broker/result/cache/rate-limit)               |
| Frontend   | Next.js 15 App Router, React 19, TanStack Query, Tailwind v4  |
| DB         | SQLite (dev) / PostgreSQL (prod) via one env var              |
| Tests      | pytest + httpx (backend), Vitest + Testing Library (web)      |
| Cloud      | Containers on AWS ECS/Fargate (or equiv), RDS Postgres, ElastiCache Redis, S3 |

---

## 2. Cloud, Redis, queues & async jobs

**Status:** the app runs today as two processes on a laptop (SQLite, mock
providers, no Redis). This section documents the **production cloud topology**
the code is structured to drop into. The seams (provider interfaces, config,
async routes) already exist; going live is configuration + deploying the worker
tier, not a rewrite.

### 2.1 Why a worker tier at all

Some steps are **slow or depend on third parties** and must not block an HTTP
request:

| Work item              | Typical latency | Runs as        |
|------------------------|-----------------|----------------|
| Resume screening (LLM) | 1–5 s           | background job |
| Availability phone call| 30–90 s (async) | job + webhook  |
| Email delivery         | 0.2–2 s         | background job |
| Teams meeting create   | 0.5–2 s         | background job |
| Interview transcription| seconds–minutes | background job |
| Interview assessment   | 1–5 s           | background job |

If these ran inline, HTTP requests would hang and time out. So the API **enqueues
a Celery task** and returns immediately (`202 Accepted` / status `queued`); the
worker does the slow work and writes the result back to the DB; the UI polls or
subscribes for the update.

### 2.2 Redis — what it's used for

Redis is a single managed instance (**AWS ElastiCache** in prod) serving four roles:

1. **Celery broker** — the task queue the API pushes jobs onto and workers pull from.
2. **Celery result backend** — stores task state/results (`queued`, `started`,
   `success`, `failure`).
3. **Cache** — memoize expensive results (e.g. a resume's screening score keyed by
   a hash of `resume_text + requirements`) with a TTL, so re-screening is instant
   and cheap.
4. **Rate limiting / idempotency locks** — token-bucket counters for outbound
   provider calls (Twilio, OpenAI) and short-lived locks so the same application
   can't be double-screened or double-called (`SETNX` with TTL).

### 2.3 Job lifecycle (generic)

```
API route  ──enqueue──▶  Redis (broker)  ──pull──▶  Celery worker
   │                                                     │
   └── returns {task_id, status:"queued"} (202)          ├── calls provider (Twilio/OpenAI/…)
                                                          ├── writes result to Postgres
   UI polls GET /jobs/{task_id}  ◀── status/result ──────┘   (screening_score, transcript, …)
                                    (Redis result backend)
```

**Retries & backoff:** each task has `max_retries` with exponential backoff
(e.g. 3 tries: 2 s → 8 s → 32 s). Transient provider errors (5xx, timeouts,
network) retry automatically; permanent errors (4xx validation) fail fast and are
recorded on the row with an error reason.

**Idempotency:** every task takes a natural key (e.g. `application_id + step`) and
acquires a Redis lock; if a duplicate is enqueued (double click, retry storm) it
is a no-op. Provider calls also pass an idempotency key where supported.

**Dead-letter:** after `max_retries`, the task lands in a dead-letter list; a
periodic sweeper surfaces failed jobs to an admin view for manual retry.

### 2.4 Cloud topology (production)

| Concern            | Service (AWS example)                | Notes                                   |
|--------------------|--------------------------------------|-----------------------------------------|
| API + workers      | ECS/Fargate (or EKS) containers      | API and Celery workers as separate services |
| Database           | RDS PostgreSQL (Multi-AZ)            | `DATABASE_URL=postgresql+asyncpg://…`   |
| Redis              | ElastiCache for Redis                | broker + result + cache + rate-limit    |
| File storage       | S3                                   | recordings, generated PDFs, resumes     |
| Secrets            | Secrets Manager / SSM Parameter Store| JWT secret, Twilio/OpenAI/MS creds      |
| Ingress/TLS        | ALB + ACM cert                       | HTTPS; also fronts Twilio/Graph webhooks|
| DNS                | Route 53                             | `PUBLIC_BASE_URL` for webhooks          |
| Logs/metrics       | CloudWatch / OpenTelemetry           | structured JSON logs (already supported)|
| CI/CD              | GitHub Actions → ECR → ECS deploy    | run pytest + vitest + build as gates    |

**Scaling:** API scales on CPU/RPS; workers scale on Redis queue depth (more
pending call/transcription jobs → more worker tasks). Postgres scales vertically +
read replicas; Redis scales with a larger node / cluster mode.

**Statelessness:** API and workers hold no local state — sessions are JWT cookies,
data is in Postgres, files in S3, queue/cache in Redis — so any container can serve
any request and autoscaling is safe.

---

## 3. Roles & access model

Four roles in the JWT `role` claim, enforced by FastAPI guards
(`require_roles(...)`). The frontend mirrors this (UX layer); the backend 403 is
the real security boundary.

| Role          | Home         | Can do                                                        |
|---------------|--------------|---------------------------------------------------------------|
| `candidate`   | `/candidate` | Own onboarding, interview stages, offer letter, chat          |
| `hr`          | `/recruiting`| Recruiting console + HR console (pipeline, offers, assets)    |
| `admin`       | `/admin`     | Employees, inventory, audit logs, role mgmt + recruiting      |
| `interviewer` | `/interviewer`| Only their assigned interviews: transcript, feedback, assessment |

Guards: `candidate_user`, `hr_user`, `admin_user`, `interviewer_user`,
`recruiting_user` (= hr **or** admin).

Demo accounts (password `password`): `admin@`, `hr@`, `interviewer@`,
`candidate@example.com`.

---

## 4. Data model

```
User ──1:0..1── Candidate ──1:N── InterviewStage      (onboarding path)
                        └──1:N── Asset

Job ──1:N── Application ──1:N── InterviewRoundRec ──1:N── Feedback (human | bot)
                     │                          ├──1:1── Transcript
                     │                          └──1:1── Assessment (suitability %)
                     └── screening_score, status, availability_confirmed

Employee, InventoryItem, AuditLog, WorkflowRun   (admin/onboarding support)
```

**Core ATS tables**

- **Job** — `title`, `department`, `description`, `requirements` (keyword list),
  `salary_display`, `shortlist_threshold` (%), `num_rounds`, `status`.
- **Application** — candidate ⇄ job; `resume_text`, `status`, `screening_score` +
  `screening_rationale`, `current_round`, `availability_confirmed` + `availability_notes`.
- **InterviewRoundRec** — `index`, `name`, `type` (`call_bot|technical|hr|executive`),
  `status`, `scheduled_at`, `meeting_url`, `interviewer_user_id`.
- **Feedback** — `source` (`human|bot`), `rating` (1–5), `recommend`, `notes`.
- **Transcript** — `text`, `source`, 1:1 with a round.
- **Assessment** — `suitability_pct`, `rationale`, `recommend_advance`, 1:1 with a round.

Schema changes are versioned Alembic migrations; a new column (e.g. `Application.phone`
for the real phone call) ships with a `server_default` so existing rows backfill safely.

---

## 5. Master hiring pipeline

```
(1) Candidate applies to a Job (resume)              status: applied
       ▼
(2) Resume screening (AI %)  → score vs requirements  status: shortlisted (≥threshold) / screening
       ▼
(3) Availability call (bot)  → confirms availability  availability_confirmed=true; status: interviewing
       ▼
(4) HR assigns interviewer (1 click) → Teams link + email to candidate; round scheduled
       ▼
(5) Interview on Teams
       ▼
(6) Transcription bot → transcript saved              round: completed
       ▼
(7) Dual feedback: human interviewer + bot (from transcript)
       ▼
(8) AI assessment → suitability % + recommend (from transcript + BOTH feedbacks)
       ▼
(9) HR decision (1 click): advance ─┐    reject → status: rejected (end)
       │  more rounds remain ────────┘ (loop to step 4)
       ▼
(10) Passed final round → status: offer
       ▼
(11) Offer letter (PDF) generated → candidate downloads
       ▼
(12) Onboarding (stages, assets, tasks)
```

HR/admin can **reject with one click at any step** and **configure the number of
rounds** per job (`num_rounds`). The AI advises; humans decide.

---

## 6. Feature pipelines (implemented)

Legend — **✅ Real:** fully working now. **🟡 Mock:** full workflow, DB writes,
and UI are real and tested; only the outbound third-party call is simulated, with
the swap-in point already coded as a provider interface (see §7).

### 6.1 Authentication & sessions — ✅ Real

**Pipeline**
1. `POST /api/auth/login` (Next.js) → forwards to FastAPI `POST /api/v1/auth/login`.
2. FastAPI finds the `User` by email, verifies the password against the stored
   **bcrypt** hash. Same 401 for unknown-user and wrong-password (no enumeration).
3. Returns a signed JWT (`sub`, `email`, `role`, `exp`).
4. Next.js sets an **httpOnly cookie** (`onboarding_at`); token never touches JS.
5. `middleware.ts` verifies JWT on each navigation; invalid/expired → `/login`.
6. `GET /api/auth/me` decodes the cookie to hydrate the React auth context.

**Production:** bcrypt hashing; httpOnly + secure cookie; startup guard refuses to
boot if JWT secret is the dev default or < 32 chars. Secret from Secrets Manager.

**Failure modes:** wrong creds → 401 (uniform message); expired token → redirect
to login; secret mismatch between web/API → all requests 401 (fixed by sharing
one secret from the same source).

### 6.2 Job postings — ✅ Real
- `GET /recruiting/jobs` (list + application counts), `POST /recruiting/jobs`
  (create), `PATCH /recruiting/jobs/{id}` (threshold, `num_rounds`, status).
- `requirements` is a comma/newline keyword list — the input to screening.

### 6.3 Application intake — ✅ Real
- `POST /recruiting/applications` (**public**) → `{job_id, applicant_name,
  applicant_email, resume_text}` → creates `Application` (`status=applied`).

### 6.4 Resume screening (AI suitability %) — ✅ Real logic (mock model default)

**Pipeline**
1. HR clicks **Screen** → `POST /recruiting/applications/{id}/screen`.
2. Loads application + job; calls
   `get_assessment_provider().score_resume(resume_text, job.requirements)`.
3. Provider returns `(suitability_pct, rationale)`.
4. Persists `screening_score` + `screening_rationale`; sets `status=shortlisted`
   if `score ≥ job.shortlist_threshold`, else `screening`.
5. UI shows a color-coded match badge (green ≥70, amber ≥50, red below).

**Production form:** screening runs as a **Celery job** (LLM latency); result
cached in Redis keyed by `hash(resume+requirements)`; real model via
`ASSESSMENT_PROVIDER=openai|gemini` (§7.5).

**Failure modes:** LLM timeout/5xx → task retries with backoff, falls back to the
deterministic mock so the pipeline never stalls; malformed model output → parser
defaults to a neutral score and flags for human review.

### 6.5 Availability call (bot) — 🟡 Mock → full Twilio pipeline in §7.1

**Pipeline today**
1. HR clicks **Call bot** → `POST /recruiting/applications/{id}/call-bot`.
2. Guard: must be `shortlisted`/`interviewing` (else 409).
3. Calls `call_bot_service.call_for_availability(...)`.
4. Persists `availability_confirmed` + `availability_notes`; creates a `call_bot`
   round (index 0, `completed`); app → `interviewing`.

Today `MockCallBotService` does **not** dial a phone — it returns a confirmed
result. The real Twilio pipeline (actual outbound call + IVR + webhooks + Celery)
is fully specified in **§7.1**.

### 6.6 Interviewer assignment (Teams link + email) — ✅ logic real / 🟡 Teams+email mock

**Pipeline (one click)**
1. `POST /recruiting/applications/{id}/assign-interviewer` with
   `{interviewer_user_id, round_name}`.
2. Validates the user has role `interviewer` (else 400).
3. Computes next human round index; 409 if it would exceed `job.num_rounds`.
4. `teams_service.create_meeting(subject)` → join URL.
5. Creates `InterviewRoundRec` (`scheduled`, `meeting_url`, interviewer); app →
   `interviewing`, `current_round` set.
6. `email_service.send(...)` emails the candidate the join link + interviewer name.

Teams + email are **mock** today (§7.2, §7.3); assignment/role/round logic is real.

### 6.7 Interview transcript — 🟡 Mock → Whisper/Azure pipeline in §7.4
1. `POST /interviewer/rounds/{id}/transcript`.
2. `transcription_service.transcribe(...)` → text.
3. `Transcript` row created/updated (1:1); round → `completed`.

### 6.8 Dual feedback (human + bot) — ✅ Real

The "automation + manual supervision" requirement: **both** opinions per round.

- **Human:** interviewer-only `POST /interviewer/rounds/{id}/feedback`
  `{rating 1–5, recommend, notes}` → `Feedback(source=human)`. Interviewers can
  only touch **their own** rounds (403 otherwise).
- **Bot:** `POST /interviewer/rounds/{id}/bot-feedback` (409 if no transcript);
  judges the transcript alone → rating `round(pct/20)`, `recommend = pct≥60`,
  `source=bot`.

### 6.9 AI assessment (suitability prediction) — ✅ Real logic (mock model default)

**Pipeline** — the "% suitable for this job" prediction.
1. HR/admin → `POST /interviewer/rounds/{id}/assess`.
2. Loads transcript + **all** feedbacks (human + bot).
3. `judge_interview(transcript, feedbacks)` → `(suitability_pct, rationale,
   recommend_advance)`; persists `Assessment` (1:1).

**Engine (mock):** blends avg rating (70%) with recommend consensus (30%);
`recommend_advance = majority recommend AND blended ≥ 60`. Split feedback (human
5/5 + bot 2/5) lands borderline and does **not** auto-recommend — human decides.

### 6.10 Round decisions & round management — ✅ Real
- `POST /recruiting/rounds/{id}/decision` `{advance|reject}`:
  - reject → round `failed`, app `rejected` (ends).
  - advance → round `passed`; if passed human rounds `≥ job.num_rounds` → app
    `offer` + offer record created.
- `POST /recruiting/applications/{id}/reject` — one-click reject any time.
- Round count set per job via `PATCH /recruiting/jobs/{id}` (`num_rounds`).

### 6.11 Offer letter (PDF) — ✅ Real
1. Clearing all rounds creates/updates an offer `Candidate` (`offer_available=true`).
2. `GET /candidate/offer-letter` (404 if no offer) → `build_offer_letter_pdf(...)`
   renders a PDF **in memory** with reportlab (name, title, salary).
3. `application/pdf` download. **Production:** store to **S3** and return a signed URL.

### 6.12 Onboarding (candidate) — ✅ Real
- `GET /candidate/me` → own profile only (onboarding status, ordered stages,
  current stage index, offer summary). No cross-candidate leakage.
- HR: stage updates with pipeline-status derivation, asset provisioning, offer
  timestamps, workflow runs — all persisted.

### 6.13 Global assistant chat — 🟡 Mock streaming → OpenAI/Gemini in §7.5
1. Widget → `POST /chat/stream` (auth required).
2. **SSE**: `data:{"text":...}` chunks then `data:{"done":true}`.
3. `get_chat_provider().stream(message)`; mock streams canned text offline; real
   model streams tokens when configured. SSE wire format identical either way.

### 6.14 Admin console — ✅ Real
- `GET /admin/employees|assets/inventory|audit-logs|users`.
- `PATCH /admin/users/{id}/role` — persists + writes an **audit-log** row. Role
  lives in the JWT, so it takes effect on the user's next login.

---

## 7. External integrations — production pipelines

Each is behind a provider interface with a working mock today. Below is the
**full production pipeline** for each — provider, cloud services, data flow,
config, retries, and failures — documented as if implemented.

### 7.1 Phone call bot (Twilio) — full production pipeline

**Purpose:** after shortlisting, an automated voice bot phones the candidate,
confirms interest + availability, and records the outcome — no recruiter time on
scheduling logistics.

**Provider:** [Twilio Programmable Voice](https://www.twilio.com/voice) for the
call + IVR; optionally Twilio ConversationRelay (or a TTS+STT agent) for natural
conversation instead of keypad.

**Cloud services used:** Twilio Voice API; Redis (Celery broker + rate-limit +
idempotency lock); Celery worker; S3 (optional call recording); Secrets Manager
(Twilio creds); ALB+Route 53 (public HTTPS for Twilio webhooks).

**Prerequisites**
- Twilio account → Account SID, Auth Token, an outbound-capable number.
- A per-candidate **phone number** — new `Application.phone` column (Alembic
  migration with `server_default=""` so existing rows backfill).
- Public HTTPS callback URL (`PUBLIC_BASE_URL`) reachable by Twilio.

**End-to-end pipeline**
```
1. HR clicks "Call bot"  →  POST /recruiting/applications/{id}/call-bot
2. API acquires Redis idempotency lock (key=app_id:callbot, TTL 5m) → 409 if held
3. API enqueues Celery task place_availability_call(app_id) → returns 202 {status:"queued"}
4. Worker: get_call_bot_service() → TwilioCallBotService.call_for_availability(name, phone)
     a. rate-limit check (Redis token bucket for Twilio)
     b. client.calls.create(
            to=phone, from_=TWILIO_FROM_NUMBER,
            url=f"{PUBLIC_BASE_URL}/api/v1/telephony/availability-twiml?app_id={id}",
            status_callback=f"{PUBLIC_BASE_URL}/api/v1/telephony/call-status?app_id={id}")
     c. writes CallAttempt row (status="dialing", call_sid=...)
5. Candidate's phone rings. Twilio GETs the TwiML:
     <Response>
       <Say>Hi {name}, this is recruiting for {job}. Are you available for
            interviews in the next two weeks? Press 1 for yes, 2 for no.</Say>
       <Gather numDigits="1" action="/api/v1/telephony/availability-result?app_id={id}"/>
     </Response>
6. Candidate presses a key → Twilio POSTs to availability-result:
     - validate Twilio request signature (X-Twilio-Signature) → 403 if invalid
     - digit 1 → confirmed; 2 → declined
     - update Application.availability_confirmed + availability_notes
     - close call_bot round (status=completed); app → interviewing (if confirmed)
7. call-status webhook records final call state (completed / no-answer / busy / failed)
8. UI refetches → shows "availability confirmed ✓"
```

Natural-language variant: step 5 streams audio to STT + a dialog agent, replies via
TTS; the webhook DB writes in steps 6–7 are unchanged.

**Config (env)**
```
CALL_BOT_PROVIDER=twilio
TWILIO_ACCOUNT_SID=ACxxxx…
TWILIO_AUTH_TOKEN=…            # from Secrets Manager in prod
TWILIO_FROM_NUMBER=+1XXXXXXXXXX
PUBLIC_BASE_URL=https://your-host
```

**Code seam (already structured)**
```python
# app/services/integrations.py
class CallBotService:                        # interface (exists)
    def call_for_availability(self, *, name, email, phone) -> CallResult: ...
class MockCallBotService(CallBotService): ...           # default today
class TwilioCallBotService(CallBotService): ...         # ← add: real client.calls.create
def get_call_bot_service() -> CallBotService:           # ← selector like chat/assessment
    if settings.call_bot_provider == "twilio" and settings.twilio_account_sid:
        return TwilioCallBotService(...)
    return MockCallBotService()
```
Plus `app/api/routes/telephony.py` (webhooks: `availability-twiml`,
`availability-result`, `call-status`) with Twilio signature validation.

**Failure modes & resolutions**

| Failure | Why | Resolution |
|---|---|---|
| No answer / busy | candidate unavailable | Celery retries N times with backoff; after max, mark `needs_manual_followup`; HR sees it and can re-trigger or email |
| Invalid phone number | bad data | Twilio 21211 → fail fast, flag application `phone_invalid`, prompt HR to correct |
| Twilio 5xx / network | provider/transient | task retry with exponential backoff (2s→8s→32s) |
| Webhook spoofing | forged POST | validate `X-Twilio-Signature`; reject 403 |
| Duplicate call (double click) | user/retry | Redis idempotency lock (`SETNX` TTL) → second call is a no-op |
| Rate limit hit | too many calls | Redis token bucket throttles; excess re-queued |
| Cost runaway | bug/loop | per-day call cap in Redis; breaker stops dialing past threshold |

**Test cases**
- Unit: `TwilioCallBotService` with a mocked Twilio client → asserts `calls.create`
  args, `CallAttempt` row written.
- Unit: `availability-result` maps digit 1→confirmed, 2→declined, updates DB, closes round.
- Security: webhook with bad signature → 403.
- Idempotency: two enqueues for same app → one call placed.
- Integration (mock provider, today): the `POST /call-bot` route confirms
  availability, creates the round, advances status (already covered by
  `test_recruiting.py::test_call_bot_requires_shortlist_then_confirms`).

**Why mock today:** real dialing needs a paid Twilio account, credentials, and a
candidate phone number. Workflow, DB writes, round creation, and UI are real now;
only dialing is simulated. Swapping in `TwilioCallBotService` + the webhook route
+ the phone migration makes it live with no route/model/UI changes beyond the phone field.

### 7.2 Email (SMTP / SendGrid / SES)

**Purpose:** interview invites (Teams link), status updates, offer notifications.

**Cloud services:** SES (or SendGrid) for delivery; Celery for async send; Redis
broker; Secrets Manager for creds.

**Pipeline**
```
Route (e.g. assign-interviewer) → enqueue send_email(to, subject, body)
Worker → get_email_service():
   MockEmailService (today): append to in-memory outbox (asserted in tests)
   SesEmailService (real):   AWS SES send → returns provider message id
                             on 4xx (bad address) fail fast; on 5xx retry w/ backoff
```

**Config:** `EMAIL_PROVIDER=ses|sendgrid|smtp`, provider creds, `EMAIL_FROM`.

**Failures:** bounce/complaint → log + mark contact; hard-bounce address flagged;
throttle via SES sending quota + Redis token bucket; retries for transient 5xx.

**Tests:** unit asserts provider `send` called with correct args; today's mock test
`test_integrations.py::test_email_records_outbox`; pipeline test asserts assign
queues one email to the candidate.

### 7.3 Teams meetings (Microsoft Graph)

**Purpose:** a real online meeting + join URL per interview round.

**Cloud services:** Microsoft Graph (Azure AD app, client-credentials OAuth2);
Secrets Manager for client secret; Celery for the create call.

**Pipeline**
```
Route (assign-interviewer) → teams_service.create_meeting(subject)
   MockTeamsService (today):  returns a teams.microsoft.com/... URL
   GraphTeamsService (real):  OAuth2 client-credentials → token (cached in Redis
                              until expiry) → POST /users/{organizer}/onlineMeetings
                              → joinWebUrl stored as round.meeting_url
```

**Config:** `TEAMS_PROVIDER=graph`, `MS_TENANT_ID`, `MS_CLIENT_ID`,
`MS_CLIENT_SECRET`, `MS_ORGANIZER_USER_ID`.

**Failures:** token expiry → refresh (cached in Redis, refreshed on 401);
Graph 429 → honor `Retry-After`, backoff; org policy blocks meeting → fall back to
a manual link field + notify HR.

**Tests:** unit with mocked Graph client asserts meeting create + URL persisted;
today's mock test asserts a unique URL per call; pipeline test asserts assignment
stores `meeting_url` and emails it.

### 7.4 Live transcription (Whisper / Azure Speech / AssemblyAI)

**Purpose:** turn interview audio into text for bot feedback + AI assessment.

**Cloud services:** a meeting-recording bot (Graph/RTMP) → audio to **S3** →
transcription provider; Celery for the (minutes-long) job; Redis for progress.

**Pipeline**
```
1. Recording bot joins the Teams call, captures audio → uploads to S3.
2. On call end → enqueue transcribe(round_id, s3_key).
3. Worker → transcription_service.transcribe(audio):
     MockTranscriptionService (today):  deterministic sample transcript
     WhisperTranscriptionService (real): Whisper/Azure/AssemblyAI → text
4. Transcript row saved (source=provider); round → completed.
5. Bot feedback + AI assessment read transcript.text unchanged.
```

**Config:** `TRANSCRIPTION_PROVIDER=whisper|azure|assemblyai` + key; `S3_BUCKET`.

**Failures:** long audio → chunked + reassembled; provider timeout → retry;
partial/low-confidence → still store, flag for human review; missing recording →
mark round `transcript_unavailable`, allow manual paste.

**Tests:** unit with mocked provider asserts transcript persisted + round completed;
today's mock test asserts transcript content; bot-feedback test asserts 409 without
a transcript then success after.

### 7.5 Real LLM (OpenAI / Gemini)

**Purpose:** replace deterministic mock scoring with real reasoning for resume
screening, interview assessment, and assistant chat.

**Cloud services:** OpenAI or Google Gemini API; Redis for response cache +
rate-limit; Celery for scoring jobs; Secrets Manager for keys.

**Pipeline (call sites unchanged; only the provider changes)**
```
Resume screening   → score_resume(resume, requirements)   → (pct, rationale)
Interview judgment → judge_interview(transcript, feedbacks)→ (pct, rationale, recommend)
Assistant chat     → stream(message)                       → SSE text chunks

Mock (today):  deterministic, offline, no key.
Real:          lazy-imported SDK; set provider + key to activate.
               Missing key → log warning, fall back to mock (never crashes).
```

**Config:** `CHAT_PROVIDER` / `ASSESSMENT_PROVIDER = openai|gemini`;
`OPENAI_API_KEY` / `GEMINI_API_KEY`; `OPENAI_MODEL` / `GEMINI_MODEL`.

**Failures:** timeout/5xx → retry then mock fallback; rate limit (429) → Redis
token bucket + queue; nondeterministic/garbled output → structured-output parsing
with a neutral default + human-review flag; cost control → cache by input hash,
per-day spend cap.

**Tests:** unit for the mock providers (deterministic scoring), provider-selector
test (mock chosen when no key), SSE format test; real providers behind a live
integration test gated on a key env var (skipped in CI by default).

---

## 8. Core feature pipelines — deep dive

§6 summarized each feature; this section gives the **detailed production
pipeline** for the core (non-external) features, at the same depth as the §7
integrations — data flow, cloud services, config, failures, and tests.

### 8.1 Authentication & session lifecycle

**Purpose:** issue and verify identity + role without exposing the token to the browser.

**Cloud services:** Secrets Manager (JWT secret); RDS Postgres (users); optional
Redis (token denylist for forced logout).

**End-to-end pipeline**
```
Login:
1. Browser POST /api/auth/login {email,password}  (Next.js route)
2. Next.js → FastAPI POST /api/v1/auth/login
3. FastAPI: SELECT user WHERE email; verify_password(pw, user.hashed_password)  [bcrypt]
     - unknown user OR bad pw → 401 "Invalid email or password" (uniform, no enumeration)
4. Build JWT {sub, email, role, exp}; sign HS256 with JWT_SECRET_KEY
5. Next.js sets httpOnly+secure+SameSite cookie "onboarding_at"; returns {ok:true}

Every authenticated request:
6. Browser → /api/backend/* (cookie auto-sent)
7. Next.js proxy reads cookie, sets Authorization: Bearer <jwt>, forwards to FastAPI
8. FastAPI dependency decodes+verifies JWT → payload{sub,role}; guards check role

Page navigation:
9. middleware.ts verifies JWT signature+exp; invalid → redirect /login?from=…

Session probe:
10. GET /api/auth/me decodes cookie server-side → {user:{id,email,role}} for React context
```

**Config:** `JWT_SECRET_KEY` (backend) == `AUTH_JWT_SECRET` (web), `JWT_ALGORITHM=HS256`,
`ACCESS_TOKEN_EXPIRE_MINUTES`. Prod: both read the same value from Secrets Manager.

**Failures & resolutions**
| Failure | Why | Resolution |
|---|---|---|
| All requests 401 | web/API secret mismatch | single secret source; startup logs the mismatch |
| Boot refused | prod secret is dev-default or <32 chars | `_validate_production_secret` guard forces a real secret |
| Token stolen via XSS | — | mitigated: httpOnly cookie, JS can't read it |
| Forced logout needed | compromised account | (prod) add JWT `jti` to a Redis denylist checked per request |
| Clock skew → early expiry | server drift | NTP-synced hosts; small `leeway` on verify |

**Tests:** `test_auth.py` (bcrypt storage, DB-backed login, hash roundtrip),
`test_smoke.py` (all-roles login, 401s, token required).

### 8.2 Resume screening (deep)

**Purpose:** rank applicants by fit so HR focuses on the best; auto-shortlist by threshold.

**Cloud services (prod):** Celery worker (LLM latency), Redis cache (memoize by
`hash(resume+requirements)`) + rate-limit, OpenAI/Gemini (real model), Postgres.

**Pipeline (production form)**
```
1. HR clicks Screen → POST /recruiting/applications/{id}/screen
2. API checks Redis cache[hash(resume+reqs)] → hit: return instantly
3. Miss → enqueue screen_resume(app_id); return 202 {status:"queued"}
4. Worker → get_assessment_provider().score_resume(resume, requirements)
     mock: keyword-overlap % ; real: LLM structured output {pct, rationale}
5. Persist screening_score + rationale; status = shortlisted (≥threshold) else screening
6. Cache result in Redis (TTL); UI refetch shows color-coded % badge
```
Today: synchronous + deterministic mock (no queue needed for instant mock).

**Failures:** LLM 5xx/timeout → retry w/ backoff → fall back to mock score;
garbled output → neutral score + `needs_review` flag; cost spikes → cache + per-day cap.

**Tests:** `test_assessment.py` (strong ≥70, weak ≤30, deterministic, empty=50),
`test_recruiting.py` (shortlist vs not by threshold).

### 8.3 Interviewer assignment (deep)

**Purpose:** one click books an interviewer, creates a real meeting, and notifies the candidate.

**Cloud services (prod):** MS Graph (Teams), SES (email), Celery (both async),
Redis (Graph token cache), Postgres.

**Pipeline**
```
1. POST /recruiting/applications/{id}/assign-interviewer {interviewer_user_id, round_name}
2. Validate target user.role == interviewer  → else 400
3. next_index = (# human rounds)+1 ; if > job.num_rounds → 409
4. teams_service.create_meeting(subject)  → joinWebUrl   (Graph in prod; mock today)
5. INSERT InterviewRoundRec(status=scheduled, meeting_url, interviewer_user_id)
6. app.status=interviewing; app.current_round=next_index
7. email_service.send(candidate, subject, body with join link + interviewer name)
8. Commit; UI shows scheduled round; candidate gets email
```

**Failures:** Graph meeting fails → round still created with a `pending_link`
state + retry job; email bounce → logged, HR notified; assigning past `num_rounds` → 409.

**Tests:** `test_recruiting.py` (assign sends email + Teams link, rejects non-interviewer 400).

### 8.4 Dual feedback (deep)

**Purpose:** capture a human opinion and an independent machine opinion per round
— automation with mandatory human supervision.

**Pipeline**
```
Human:  POST /interviewer/rounds/{id}/feedback {rating 1-5, recommend, notes}
        guard: interviewer_user AND round.interviewer_user_id == me  → else 403
        INSERT Feedback(source=human, author_user_id=me)

Bot:    POST /interviewer/rounds/{id}/bot-feedback
        409 if no transcript yet
        judge_interview(transcript, [])  → pct
        INSERT Feedback(source=bot, rating=round(pct/20), recommend=pct>=60)
```

**Failures:** interviewer edits someone else's round → 403; bot feedback before
transcript → 409 (clear message to generate transcript first); duplicate submits
→ allowed (multiple feedbacks aggregate) or de-duped by policy.

**Tests:** `test_interviewer.py` (human+bot feedback, 403 for non-owner, 409 without transcript).

### 8.5 AI assessment (deep)

**Purpose:** the headline prediction — "% suitable for this job" — from transcript
+ both feedbacks, plus an advance recommendation.

**Cloud services (prod):** Celery, Redis, OpenAI/Gemini, Postgres.

**Pipeline**
```
1. HR/admin → POST /interviewer/rounds/{id}/assess
2. Load transcript + ALL feedbacks (human + bot)
3. judge_interview(transcript, feedbacks):
     mock: blended = 0.7*avgRating(0-100) + 0.3*recommendRatio*100
           recommend_advance = recommendRatio>=0.5 AND blended>=60
     real: LLM reasons over transcript+feedback → {pct, rationale, recommend}
4. UPSERT Assessment(suitability_pct, rationale, recommend_advance)  [1:1 with round]
5. UI shows big % + rationale + advance recommendation
```
Split feedback (human 5 / bot 2) → borderline, does **not** auto-recommend; HR decides.

**Failures:** no transcript/feedback → provisional low score, no recommend; LLM
failure → mock fallback; bias/fairness → rationale is always shown for human audit.

**Tests:** `test_assessment.py` (positive→recommend, negative→reject, split→borderline),
`test_interviewer.py` (assess after transcript+dual feedback).

### 8.6 Round decisions & offer trigger (deep)

**Pipeline**
```
POST /recruiting/rounds/{id}/decision {advance|reject}
  reject  → round.status=failed; application.status=rejected  (pipeline ends)
  advance → round.status=passed
            passedHuman = count(rounds where type!=call_bot AND status=passed)
            if passedHuman >= job.num_rounds:
                application.status=offer
                _create_offer_for_application()  → Candidate(offer_available=true)
POST /recruiting/applications/{id}/reject → application.status=rejected (any time)
job.num_rounds editable via PATCH /recruiting/jobs/{id}
```

**Failures:** advancing an already-decided round → guarded/idempotent; race between
two HR users → last write wins on the row, both see refreshed state.

**Tests:** `test_recruiting.py::test_full_pipeline_to_offer` (advance final round → offer),
one-click reject.

### 8.7 Offer letter (PDF) with cloud storage (deep)

**Pipeline**
```
1. Offer created when all rounds passed (Candidate.offer_available=true)
2. Candidate → GET /candidate/offer-letter   (404 if no offer)
3. build_offer_letter_pdf(name, title, salary) → PDF bytes (reportlab, in-memory)
4a. Dev: return application/pdf directly (attachment; filename=offer-letter.pdf)
4b. Prod: put bytes to S3 (s3://bucket/offers/{candidate}.pdf), return a
    time-limited signed URL; audit the download
```

**Failures:** no offer yet → 404; special characters in name → handled (tested);
S3 outage (prod) → retry, fall back to direct stream.

**Tests:** `test_documents.py` (valid PDF, special chars), `test_candidate.py`
(PDF download, 404 when no offer).

### 8.8 Onboarding (deep)

**Pipeline**
```
Candidate GET /candidate/me → own profile only (user_id-scoped):
  onboarding_status, ordered interview_stages, current_stage_index, offer summary
HR side (/hr console):
  - PATCH interview stage status → derive pipeline status (in_progress/pending/complete)
  - provision assets (status transitions)
  - generate offer (timestamp)
  - start onboarding workflow runs (persisted)
```

**Failures:** cross-candidate access → blocked by user_id scoping (tested); no
profile for a candidate user → 404.

**Tests:** `test_candidate.py` (own-profile isolation, no-profile 404), `test_hr.py`
(stage transitions, workflow persistence).

### 8.9 Global assistant chat (deep)

**Pipeline**
```
1. Widget → POST /chat/stream {message}  (auth required)
2. Server streams SSE:  data:{"text":"…"}  (repeated)  then  data:{"done":true}
3. get_chat_provider().stream(message):
     mock: chunks a canned reply offline
     real: OpenAI/Gemini streaming tokens (lazy SDK), same SSE frames
4. Browser reader appends chunks live; toast on error
```

**Failures:** unauth → 401/403; provider timeout → error toast, partial text kept;
missing key → mock fallback. **Prod:** per-user rate limit in Redis; PII scrubbing
before sending to the model.

**Tests:** `test_chat.py` (SSE format, auth required, mock determinism, provider selector).

### 8.10 Admin console & audit (deep)

**Pipeline**
```
GET /admin/employees | /admin/assets/inventory | /admin/audit-logs | /admin/users
PATCH /admin/users/{id}/role {role}:
  - UPDATE users.role
  - INSERT AuditLog(action="admin.role_change", detail="old -> new", actor_email)
  - role change takes effect on the user's NEXT login (role is in the signed JWT)
```

**Failures:** unknown user → 404; privilege escalation attempt by non-admin → 403;
every mutation writes an immutable audit row for compliance.

**Tests:** `test_admin.py` (list, sorted users, role change persists + audits, 404).

---

## 9. Cross-cutting concerns

Applies across all features.

### 9.1 Observability
- **Logging:** structured JSON logs (`LOG_JSON=true`) with request id, user id,
  route, latency; shipped to CloudWatch/OpenTelemetry. SQL echo behind `DB_ECHO`.
- **Metrics:** request rate/latency/error %, Celery queue depth, task success/
  failure, provider latency, LLM token/cost counters.
- **Tracing:** OpenTelemetry spans across API → worker → provider for a full
  request/job trace.
- **Alerts:** on 5xx spikes, queue backlog, DLQ growth, provider error rate,
  daily cost cap approach.

### 9.2 Security & compliance
- bcrypt password hashing; httpOnly+secure JWT cookie; prod secret guard.
- Server-side role enforcement (403); webhook signature validation.
- Secrets only in Secrets Manager/SSM (never in code or images).
- Input validation via Pydantic; parameterized queries via SQLAlchemy (no injection).
- Untrusted content (resumes, transcripts, webhook bodies) treated as data, never
  executed; PII scrubbed before LLM calls.
- Audit log for sensitive actions (role changes, offer generation, rejections).

### 9.3 Data lifecycle & retention
- Candidate PII retained per policy window, then purged/anonymized (GDPR/CCPA).
- Recordings/transcripts stored in S3 with lifecycle rules (expire after N days).
- Right-to-erasure: cascade delete/anonymize by candidate across applications,
  rounds, feedback, transcripts, assessments.
- Backups: RDS automated snapshots (Multi-AZ), point-in-time recovery.

### 9.4 Performance & scaling
- N+1 queries avoided with `selectinload` (candidate stages, application rounds).
- API scales on CPU/RPS; workers scale on Redis queue depth.
- Redis caching for screening/LLM; DB read replicas for heavy read paths.
- Stateless services → horizontal autoscaling is safe.

### 9.5 Deployment & rollback
- CI (GitHub Actions): pytest + vitest + `npm run build` + Alembic head check.
- Build images → ECR → deploy to ECS (rolling or blue/green).
- Migrations run on deploy (`alembic upgrade head`); reversible via `downgrade`.
- Rollback: redeploy previous image tag; migrations designed with safe down paths.

---

## 10. Failure modes & resolutions (catalog)

Platform-wide failure handling, beyond the per-integration tables in §7 and §8.

| # | Failure | Why it happens | How it's resolved |
|---|---------|----------------|-------------------|
| 1 | JWT secret mismatch (web vs API) | two secrets configured | single source (Secrets Manager); both read the same value; symptom = all 401s |
| 2 | Expired/invalid JWT | token TTL passed | middleware redirects to `/login`; refresh on re-login |
| 3 | Cross-role access attempt | user hits another role's API | backend `require_roles` → 403; frontend hides the nav (UX) |
| 4 | DB connection drop | Postgres failover / network | `pool_pre_ping` recycles dead connections; request retried by client; Multi-AZ failover |
| 5 | Migration drift | model changed, no migration | Alembic autogenerate + `upgrade head` on deploy; CI checks head is current |
| 6 | NOT-NULL column added to existing rows | naive migration | add with `server_default` (done for `offer_document_format`) so rows backfill |
| 7 | Slow external call blocks request | inline provider call | moved to Celery job; API returns 202; UI polls |
| 8 | Provider 5xx / timeout | third-party outage | task retries w/ exponential backoff; fall back to mock where safe (LLM) |
| 9 | Duplicate action (double click) | user/retry | Redis idempotency lock (`SETNX` TTL); provider idempotency keys |
| 10 | Retry storm / thundering herd | many retries at once | jittered backoff; per-provider rate limit (Redis token bucket) |
| 11 | Poison job (always fails) | permanent bad input | after `max_retries` → dead-letter list; admin retry view |
| 12 | Webhook spoofing | forged provider callback | validate signatures (Twilio `X-Twilio-Signature`, etc.) → 403 |
| 13 | Cost runaway (calls/LLM) | loop/bug | per-day caps + circuit breaker in Redis; alerts |
| 14 | Candidate data leak across tenants | query bug | every candidate/interviewer query scoped by `user_id`/assignment; tested |
| 15 | SQLite file lock (dev) | server holds DB during reseed | stop uvicorn before delete/reseed (dev-only; Postgres has no file lock) |
| 16 | Frontend build/type error | bad TS | `npm run build` is the CI gate (all routes must compile) |
| 17 | Worker queue backlog | spike in jobs | autoscale workers on Redis queue depth |
| 18 | Lost in-memory state (mock email/outbox) | process restart | acceptable for mocks; real providers persist to their systems/S3/DB |

---

## 11. Test strategy & cases

**Current suite: 66 backend (pytest) + 16 frontend (Vitest) = 82, all green.**

**Run**
```powershell
.\.venv\Scripts\python.exe -m pytest        # backend, 66
cd web; npm test                            # frontend, 16
cd web; npm run build                       # type/route gate
```

**Backend coverage (by file)**
- `test_smoke.py` — health, login all roles, bad/unknown login → 401, token
  required, cross-role → 403, each role reads own data.
- `test_auth.py` — passwords stored as bcrypt (not plaintext), login validates
  against DB, hash roundtrip.
- `test_hr.py` — candidate list + shape, current-stage derivation, stage patch
  persists + demotes others, all-done → ready_for_offer, offer + workflow persist, 404.
- `test_admin.py` — employees/inventory/users, role change persists + audits, 404.
- `test_candidate.py` — own-profile isolation (no cross-candidate leak), PDF
  offer download, offer 404 when none, no-profile 404.
- `test_chat.py` — SSE format, auth required, provider abstraction, mock determinism.
- `test_documents.py` — valid PDF bytes + special characters.
- `test_assessment.py` — strong resume ≥70, weak ≤30, deterministic, empty reqs
  neutral, feedback aggregation (positive→recommend, negative→reject, split→borderline).
- `test_integrations.py` — email outbox, unique Teams URL, call-bot confirms,
  transcription content.
- `test_recruiting.py` — list jobs, candidate denied 403, screen shortlists /
  not, call-bot 409-then-confirm, assign sends email + Teams link, assign rejects
  non-interviewer, one-click reject, **full pipeline apply→screen→callbot→assign→advance→offer**.
- `test_interviewer.py` — interviewer sees own assignment, hr 403 on interviewer
  endpoint, transcript→human+bot feedback→assessment→detail, bot-feedback 409
  without transcript, feedback requires interviewer role.

**Frontend coverage**
- `navigation.test.ts` — role → visible areas, `canAccess`, home routing.
- `role-guard.test.tsx` — loading / allowed / denied / no-user states.

**Production test additions (per §7)** — unit tests with mocked provider SDKs
(Twilio/SES/Graph/Whisper/OpenAI), webhook signature-validation tests,
idempotency tests, and key-gated live integration tests (skipped in CI).

**CI gates:** GitHub Actions runs pytest + vitest + `npm run build` on every push;
Alembic head-currency check; deploy to ECS only if all pass.

---

## 12. Production readiness checklist

- [x] Async API + DB, provider pattern for all external calls
- [x] Alembic migrations own schema; safe column adds (`server_default`)
- [x] bcrypt hashing, httpOnly cookie, prod secret guard
- [x] Role-based access enforced server-side (403) + mirrored in UI
- [x] 82 automated tests; build gate
- [x] Structured JSON logging supported (`LOG_JSON`)
- [ ] Deploy worker tier (Celery) + Redis (ElastiCache) — code seams ready
- [ ] Swap mock providers → Twilio / SES / Graph / Whisper / OpenAI (config)
- [ ] Postgres (RDS) via `DATABASE_URL`; S3 for PDFs/recordings; signed URLs
- [ ] Secrets in Secrets Manager; webhooks behind HTTPS with signature validation
- [ ] Rate limits, idempotency locks, cost caps, circuit breakers in Redis
- [ ] Dead-letter + admin retry view; alerting on queue depth / failures
- [ ] CI/CD: GitHub Actions → ECR → ECS; blue/green or rolling deploy

---

## 13. Configuration reference

**Backend `.env`** (dev defaults; all optional locally)
```
APP_ENV=development
DATABASE_URL=sqlite+aiosqlite:///./onboarding.db     # prod: postgresql+asyncpg://…
JWT_SECRET_KEY=…                                     # must match web AUTH_JWT_SECRET
CHAT_PROVIDER=mock                                   # mock|openai|gemini
ASSESSMENT_PROVIDER=mock                             # mock|openai|gemini
# Worker/cloud (prod):
# REDIS_URL=redis://…                 CELERY_BROKER_URL / CELERY_RESULT_BACKEND
# S3_BUCKET=…                         PUBLIC_BASE_URL=https://your-host
# CALL_BOT_PROVIDER, TWILIO_*         (§7.1)
# EMAIL_PROVIDER, SES_/SENDGRID_/SMTP_*  (§7.2)
# TEAMS_PROVIDER, MS_*                (§7.3)
# TRANSCRIPTION_PROVIDER, provider key   (§7.4)
# OPENAI_API_KEY / GEMINI_API_KEY     (§7.5)
```

**Web `web/.env.local`**
```
BACKEND_INTERNAL_URL=http://127.0.0.1:8000
AUTH_JWT_SECRET=…            # must equal backend JWT_SECRET_KEY (HS256)
```

**Run locally**
```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
cd web; npm run dev
# Prod additionally:  celery -A app.worker worker  +  a running Redis
```

---

## 14. Status glossary & implementation matrix

**Application status:** `applied` → `screening`/`shortlisted` → `interviewing` →
`offer`, or `rejected` anytime.
**Round status:** `pending` → `scheduled` → `completed` → `passed`/`failed`.
**Feedback source:** `human` | `bot`. **Round types:** `call_bot`, `technical`,
`hr`, `executive`.

| Feature | Status |
|---|---|
| Auth, roles, sessions | ✅ Real |
| Jobs, applications | ✅ Real |
| Resume screening (%) | ✅ Real logic (mock model; real via §7.5) |
| Availability phone call | 🟡 Mock → Twilio pipeline (§7.1) |
| Interviewer assignment | ✅ Real logic |
| Teams meeting link | 🟡 Mock → Graph pipeline (§7.3) |
| Email notifications | 🟡 Mock → SES/SMTP pipeline (§7.2) |
| Transcript | 🟡 Mock → Whisper pipeline (§7.4) |
| Dual feedback (human + bot) | ✅ Real |
| AI assessment (%) | ✅ Real logic (mock model; real via §7.5) |
| Round decisions & management | ✅ Real |
| Offer letter (PDF) | ✅ Real (prod: store to S3) |
| Onboarding + admin console | ✅ Real |
| Assistant chat (streaming) | 🟡 Mock → OpenAI/Gemini (§7.5) |
| Worker tier (Celery) + Redis + cloud | ⬜ Documented + code-seams ready |

_"Mock" = full workflow, data flow, persistence, and UI are real and tested
today; only the outbound call to a paid third party is simulated, and the swap-in
point is already coded as a provider interface._

---

_Generated as the authoritative pipeline reference. Keep in sync with code as
integrations move from 🟡 mock to ✅ real._
