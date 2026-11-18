# Automate Interview

**AI-powered recruitment and end-to-end hiring automation platform**

Automate Interview streamlines the hiring lifecycle, from job posting
and candidate applications to resume screening, interview coordination,
feedback, offer generation, and onboarding.

> **Implementation note:** Core application workflows are implemented
> and tested. Several external services (phone calls, Teams meetings,
> email delivery, transcription, and live LLM calls) currently use mock
> providers locally. The production integrations and AWS deployment
> described below are the planned architecture, not a claim that those
> services are already deployed.

## Table of Contents

-   [Overview](#overview)
-   [Hiring Workflow](#hiring-workflow)
-   [Features](#features)
-   [User Roles](#user-roles)
-   [Architecture](#architecture)
-   [Technology Stack](#technology-stack)
-   [AI Assessment](#ai-assessment)
-   [Security](#security)
-   [Testing](#testing)
-   [Local Setup](#local-setup)
-   [Provider and Deployment Status](#provider-and-deployment-status)
-   [Planned Production Integrations](#planned-production-integrations)

## Overview

The platform brings candidates, HR teams, interviewers, and
administrators into one recruitment workflow.

It supports:

-   Job creation and configuration of screening thresholds and interview
    rounds.
-   Candidate application intake and resume screening.
-   Interviewer assignment and interview-round tracking.
-   Human and AI-assisted interview feedback.
-   AI-generated suitability assessments to support HR decisions.
-   PDF offer-letter generation and candidate onboarding.
-   Role-specific dashboards and administrative audit records.

The AI provides recommendations and supporting rationale. **Final hiring
decisions remain with HR.**

## Hiring Workflow

``` mermaid
flowchart TD
    A[Job is published] --> B[Candidate submits application and resume]
    B --> C[Resume screening]
    C --> D{Meets shortlist threshold?}
    D -- No --> E[HR reviews application]
    D -- Yes --> F[Availability confirmation]
    E --> F
    F --> G[HR assigns interviewer]
    G --> H[Interview round]
    H --> I[Transcript and feedback]
    I --> J[AI assessment]
    J --> K{HR decision}
    K -- Reject --> L[Application rejected]
    K -- Advance --> M{More rounds?}
    M -- Yes --> G
    M -- No --> N[Offer generated]
    N --> O[PDF offer letter]
    O --> P[Candidate onboarding]
```

HR and administrators can reject an application at any stage and
configure the number of interview rounds for a job.

## Features

### 1. Recruitment and job management

-   Create and manage job postings.
-   Configure job requirements, shortlist thresholds, and
    interview-round counts.
-   Track applications and their current recruitment status.

### 2. Candidate application and resume screening

-   Accept candidate details and resume text through the application
    workflow.
-   Compare resume content against job requirements.
-   Generate a suitability percentage and rationale.
-   Shortlist applications according to the configured threshold.

The current default assessment provider uses deterministic mock scoring.
A provider interface is available for connecting a real LLM provider.

### 3. Availability workflow

-   HR can trigger the availability workflow for shortlisted candidates.
-   The result and availability notes are stored against the
    application.
-   The current local implementation uses a mock call-bot provider; it
    does not place a real phone call.

### 4. Interview scheduling

-   Assign an interviewer to an application.
-   Validate that the selected user has the interviewer role.
-   Create and track interview rounds.
-   Attach a meeting URL and notify the candidate through the
    email-provider interface.

The assignment and round-management logic is implemented. Teams meeting
creation and outbound email are mocked in the local setup.

### 5. Interview transcripts and feedback

-   Store interview transcripts against their respective rounds.
-   Capture interviewer feedback, including rating, recommendation, and
    notes.
-   Generate bot feedback from the transcript.
-   Keep human and AI feedback distinguishable.

The current transcription provider is a mock implementation.

### 6. AI interview assessment

-   Evaluate the transcript and available feedback.
-   Produce a suitability percentage, rationale, and recommendation.
-   Store the assessment against the interview round.
-   Allow HR to use the assessment as decision support.

### 7. Offer letters and onboarding

-   Generate downloadable PDF offer letters.
-   Track candidate onboarding stages and progress.
-   Support HR-side workflow and asset management.

### 8. Role-based dashboards

The application supports separate experiences for candidates, HR users,
interviewers, and administrators.

### 9. Global assistant

The chat interface streams responses using Server-Sent Events (SSE). A
mock provider is available for local development, with provider
interfaces for OpenAI or Gemini integration.

## User Roles

  -----------------------------------------------------------------------
  Role                                Main responsibilities
  ----------------------------------- -----------------------------------
  Candidate                           View interview stages, onboarding
                                      progress, and available offer
                                      documents

  HR                                  Manage jobs, applications,
                                      interviews, decisions, offers, and
                                      onboarding

  Interviewer                         Access assigned interviews,
                                      transcripts, and feedback workflows

  Admin                               Manage users and roles, employees,
                                      inventory, and audit logs
  -----------------------------------------------------------------------

Authorization is enforced by the backend. Frontend route and navigation
restrictions are an additional user-experience layer, not the security
boundary.

## Architecture

The application uses a Next.js frontend and a FastAPI backend. The
Next.js server acts as a Backend-for-Frontend (BFF), forwarding
authenticated requests to FastAPI.

``` mermaid
flowchart LR
    A[Browser<br/>React and Next.js] --> B[Next.js BFF]
    B -->|JWT Bearer token| C[FastAPI]
    C --> D[SQLAlchemy Async]
    D --> E[(SQLite for development<br/>PostgreSQL for production)]
    C -. planned background jobs .-> F[Celery Workers]
    F <--> G[(Redis)]
    F -. provider interfaces .-> H[External Services]
```

### Key design decisions

-   **Backend-for-Frontend:** the browser uses an HTTP-only
    authentication cookie; the Next.js proxy forwards the JWT to
    FastAPI.
-   **Backend authorization:** role checks are enforced by FastAPI
    endpoints.
-   **Asynchronous backend:** FastAPI and SQLAlchemy use asynchronous
    request and database patterns.
-   **Provider abstraction:** external capabilities such as calling,
    email, meetings, transcription, and AI can be swapped through
    provider interfaces.
-   **Database migrations:** Alembic manages schema changes.
-   **Background processing architecture:** Celery and Redis are
    specified for slow or external tasks in the production design. They
    are not part of the current local two-process setup.

## Technology Stack

  -----------------------------------------------------------------------
  Layer                               Technologies
  ----------------------------------- -----------------------------------
  Frontend                            Next.js 15, React 19, TypeScript,
                                      Tailwind CSS v4

  Data fetching                       TanStack Query

  Backend                             Python, FastAPI, Pydantic v2

  Database access                     SQLAlchemy 2.0 async

  Migrations                          Alembic

  Authentication                      JWT (HS256), bcrypt

  Development database                SQLite

  Production database design          PostgreSQL / Amazon RDS

  PDF generation                      ReportLab

  Testing                             Pytest, HTTPX, Vitest, Testing
                                      Library

  Production architecture             AWS ECS/Fargate, RDS, ElastiCache,
                                      S3, Secrets Manager
  -----------------------------------------------------------------------

## AI Assessment

The application uses provider interfaces so deterministic local behavior
can be replaced by a real model without changing the feature call sites.

### Resume screening

The assessment provider receives resume text and job requirements and
returns a suitability percentage with a rationale.

### Interview assessment

The mock assessment combines the average feedback rating and
recommendation consensus:

``` text
blended_score = 0.7 * average_rating_score
              + 0.3 * recommendation_ratio_score
```

The mock recommendation requires recommendation consensus of at least
50% and a blended score of at least 60.

A real OpenAI or Gemini provider can be selected through configuration
when credentials are supplied. The mock provider remains the local
fallback.

## Security

The documented security controls include:

-   Password hashing with bcrypt.
-   JWT authentication using HTTP-only cookies.
-   Server-side role-based access control.
-   Pydantic request validation.
-   Parameterized database operations through SQLAlchemy.
-   Production secret validation.
-   Audit records for sensitive administrative actions.
-   Provider webhook signature validation in the production integration
    design.
-   Secret storage through a managed secrets service in production.

## Testing

The project pipeline document reports **82 passing automated tests**:

-   **66 backend tests** using Pytest.
-   **16 frontend tests** using Vitest and Testing Library.

The test coverage includes authentication, role access, recruitment
workflows, resume assessment, interviewer feedback, candidate isolation,
PDF generation, integrations using mocks, and frontend role-based
navigation.

Run the test suites:

``` bash
# Backend
python -m pytest

# Frontend
cd web
npm test

# Frontend production build
npm run build
```

## Local Setup

The following commands reflect the documented local development
workflow. Run them from the relevant project directories.

### 1. Backend

``` bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

Create the backend `.env` file and configure the required values.
Example:

``` env
DATABASE_URL=sqlite+aiosqlite:///./app.db
JWT_SECRET_KEY=replace-with-a-long-random-secret
CHAT_PROVIDER=mock
ASSESSMENT_PROVIDER=mock
```

Start the API:

``` bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 2. Frontend

In a second terminal:

``` bash
cd web
npm install
```

Create `web/.env.local`:

``` env
BACKEND_INTERNAL_URL=http://127.0.0.1:8000
AUTH_JWT_SECRET=use-the-same-value-as-JWT_SECRET_KEY
```

Start the frontend:

``` bash
npm run dev
```

Open `http://localhost:3000` in your browser.

**Important:** `JWT_SECRET_KEY` in the backend and `AUTH_JWT_SECRET` in
the frontend must use the same value.

## Provider and Deployment Status

  -----------------------------------------------------------------------
  Component                           Current local status
  ----------------------------------- -----------------------------------
  Authentication and sessions         Implemented

  Job postings and application intake Implemented

  Resume screening workflow           Implemented; deterministic mock
                                      assessment by default

  Interviewer assignment and round    Implemented
  logic                               

  Teams meeting creation              Mock provider

  Candidate email notifications       Mock provider

  Availability phone call             Mock provider; no real outbound
                                      call

  Interview transcription             Mock provider

  Human and bot feedback workflow     Implemented; bot feedback uses the
                                      assessment provider

  AI interview assessment             Implemented; deterministic mock
                                      provider by default

  PDF offer generation                Implemented

  Candidate onboarding workflows      Implemented

  Admin console and audit records     Implemented

  Live OpenAI/Gemini providers        Provider integration path
                                      documented; mock is the default

  Celery and Redis worker             Production architecture documented;
  infrastructure                      not part of the local setup

  AWS deployment and managed services Production architecture documented;
                                      deployment not claimed
  -----------------------------------------------------------------------

## Planned Production Integrations

The pipeline document describes the following production integration
paths:

-   **Twilio:** outbound availability calls, IVR, and webhook callbacks.
-   **Microsoft Graph:** Teams meeting creation and meeting-related
    workflows.
-   **Email provider:** production candidate notifications, such as
    Amazon SES.
-   **Transcription provider:** Whisper, Azure Speech, or AssemblyAI.
-   **OpenAI / Gemini:** live resume screening, interview assessment,
    and assistant responses.
-   **Celery + Redis:** background jobs, retries, caching, rate
    limiting, and idempotency.
-   **AWS services:** ECS/Fargate, RDS PostgreSQL, ElastiCache Redis,
    S3, Secrets Manager, Application Load Balancer, and CloudWatch.

These are production design targets. They should be configured and
deployed before being described as live integrations.

------------------------------------------------------------------------

## Project Goal

Automate Interview aims to make recruitment workflows more organized and
efficient while keeping hiring decisions under human control. It
combines role-based workflow management, AI-assisted evaluation,
interview coordination, and onboarding in one platform.
