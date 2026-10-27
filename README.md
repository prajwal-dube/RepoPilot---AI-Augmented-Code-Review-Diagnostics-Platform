# RepoPilot

RepoPilot reviews GitHub pull requests with two cooperating agents. The Auditor checks added lines for security and correctness patterns, then optionally asks an LLM for a contextual review. The Diagnostician interprets supplied profiler/log samples and flags regressions. Qdrant supplies retrieved architecture notes and resolved issues. A FastAPI webhook enqueues work through Celery/Redis; the worker fetches PR files, runs the LangGraph workflow, and updates one PR comment.

This is a runnable portfolio implementation, not an enterprise certification. It never executes code from a PR. Static findings are heuristics; LLM findings require human verification.

## Requirements

- Docker Desktop using Linux containers, or Docker Engine with Compose v2.
- Python 3.11 or newer to run tests or the benchmark CLI outside Docker.
- A GitHub webhook secret is required to accept webhook requests. A `GITHUB_TOKEN` is optional for public PR reads, but required to read private repositories and publish comments.
- An `OPENAI_API_KEY` is optional. Without it, no LLM review or semantic embeddings are used; Qdrant uses local lexical vectors.

## Configure and run

Copy the example environment file and replace both secret placeholders with independent random values before starting:

```powershell
Copy-Item .env.example .env
$webhookSecret = python -c "import secrets; print(secrets.token_urlsafe(48))"
$adminKey = python -c "import secrets; print(secrets.token_urlsafe(48))"
$envLines = Get-Content .env
$envLines = $envLines -replace '^GITHUB_WEBHOOK_SECRET=.*$', "GITHUB_WEBHOOK_SECRET=$webhookSecret"
$envLines = $envLines -replace '^REPOPILOT_ADMIN_KEY=.*$', "REPOPILOT_ADMIN_KEY=$adminKey"
$envLines | Set-Content .env
```

Set `GITHUB_TOKEN` and/or `OPENAI_API_KEY` in `.env` only when needed. Keep `.env` private; it is ignored by Git. The manual analysis, result, and context-index routes return `503` when `REPOPILOT_ADMIN_KEY` is missing, and `401` for an incorrect key.

Start and verify the complete stack:

```powershell
docker compose up --build -d
docker compose ps
curl.exe -f http://127.0.0.1:8000/health
docker compose exec -T api celery -A repopilot.tasks.celery_app inspect ping --timeout=10
docker compose exec -T api python -c "import redis,urllib.request; print('redis=', redis.Redis.from_url('redis://redis:6379/0').ping()); print('qdrant=', urllib.request.urlopen('http://qdrant:6333/healthz', timeout=5).status)"
```

The API is bound to `127.0.0.1:8000`; Redis and Qdrant are internal to Compose. `docker compose down` stops the stack and preserves its data volumes. Do not expose the API publicly without TLS, a perimeter control, a strong webhook secret, and scoped GitHub credentials.

To configure GitHub, add a webhook at `https://YOUR_HOST/webhooks/github`, select **Pull requests**, and use the same `GITHUB_WEBHOOK_SECRET`. The token needs permission to read pull requests and write issue comments in the target repository. RepoPilot does not create or verify an externally delivered webhook in the local test described below.

## API

- `POST /webhooks/github`: HMAC SHA-256 verified `pull_request` events (`opened`, `synchronize`, `reopened`, `ready_for_review`); duplicate delivery IDs are deduplicated in Redis.
- `POST /analyses`: enqueue supplied diff data. Requires `X-RepoPilot-Key: <REPOPILOT_ADMIN_KEY>`.
- `GET /analyses/{task_id}`: retrieve state and result. Requires the same admin key.
- `POST /context`: add a document to Qdrant. Requires the same admin key.
- `GET /health`: API process health.

Example manual request:

```powershell
curl.exe -X POST http://127.0.0.1:8000/analyses `
  -H "Content-Type: application/json" `
  -H "X-RepoPilot-Key: $env:REPOPILOT_ADMIN_KEY" `
  -d '{"repository":"org/repo","pull_number":1,"files":[{"path":"app.py","patch":"@@ -1 +1 @@\n+password = ''demo''"}]}'
```

The worker fetches paginated GitHub file patches and caps processing at 300 changed files and 450 KB of patch text. Missing patches and over-limit files are reported as skipped. Comments use a stable marker, serialize updates per PR with a Redis lock, and scan up to 20 pages before creating a new comment; if the scan cannot establish whether an existing comment is present, the task fails rather than risk posting a duplicate. GitHub network errors, `429`/`5xx` responses, and rate-limited `403` responses are retried up to three times with backoff; ordinary permission-related `403` errors are not retried. The task is acknowledged only after processing.

PR diffs and retrieved context are untrusted input. They are passed as data to the optional LLM with explicit instructions not to follow embedded instructions; model findings are limited to added lines. No PR code is executed. Comments escape `@` mentions to avoid triggering notifications from untrusted review text.

## Tests

Create an isolated environment and run the full unit/integration suite:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The suite includes signature validation, malformed/oversized webhooks, admin-route access control, delivery deduplication, GitHub auth and pagination, duplicate-comment protection, retry configuration, Qdrant retrieval, and the two-agent workflow. Verified in this workspace: **20 tests passed**.

## End-to-end benchmark

`repopilot.benchmark` supports a real signed-webhook-to-Celery-result run against the running stack. The benchmark fetches PR size metadata before timing, posts a locally signed sample event for each supplied PR number, and polls the protected result endpoint until completion. It records each delivery/task ID, diff size, terminal state, latency, and result. It does not post a comment unless the worker has a write-capable `GITHUB_TOKEN`.

Run from PowerShell after setting the two shell variables to the values in `.env`:

```powershell
$env:GITHUB_WEBHOOK_SECRET = (Get-Content .env | Where-Object { $_ -like 'GITHUB_WEBHOOK_SECRET=*' }) -replace '^GITHUB_WEBHOOK_SECRET=', ''
$env:REPOPILOT_ADMIN_KEY = (Get-Content .env | Where-Object { $_ -like 'REPOPILOT_ADMIN_KEY=*' }) -replace '^REPOPILOT_ADMIN_KEY=', ''
.\.venv\Scripts\python.exe -m repopilot.benchmark `
  --api-url http://127.0.0.1:8000 `
  --repository OWNER/REPO `
  --pulls 123 456 789 `
  --timeout 300 `
  --output benchmark.json
```

### Verified run in this workspace

| Measure | Result |
|---|---:|
| Successful jobs / samples | 3 / 3 |
| Median webhook-to-result | 1.0704 s |
| p95 webhook-to-result (nearest-rank; n=3) | 3.7682 s |
| PR diffs | `psf/requests#7583`: 1 file, +1/-1; `#7577`: 5 files, +83/-3; `#7575`: 9 files, +148/-24 |
| Test environment | Windows 10 / WSL2; Docker Desktop engine 29.8.0; API/worker Python 3.12; local benchmark Python 3.11.3; Redis 7.4.11; Qdrant server 1.12.6 and client 1.12.2 |
| GitHub token / OpenAI key | Not configured / not configured |

All three PRs were closed public PRs read from the unauthenticated GitHub API. The benchmark sent synthetic, correctly HMAC-signed `synchronize` payloads to the local webhook; this verified webhook → Redis/Celery → worker → GitHub file API → LangGraph → Qdrant → result retrieval. A temporary Qdrant smoke document was used to verify retrieval and removed after the benchmark. It is **not** a live webhook delivery from GitHub, a private-repository test, or a comment-publishing test. No GitHub or LLM credentials were supplied. The raw output is in [`benchmark.json`](./benchmark.json).

Latency summaries use successful jobs only; the raw records retain each delivery state and failure. This sample is small and includes one 3.7682-second outlier. It is not a production latency guarantee. It does not support a sub-45-second claim, and no 40% LLM cost-reduction claim was measured.

### LLM usage and cost

No `OPENAI_API_KEY` was configured for the verified run. LLM token usage, billed cost, and with-versus-without-retrieval cost were therefore **not measured**. The worker does not currently return provider usage or billing data. The no-credential run made no LLM calls and incurred no LLM token cost. The default local microbenchmark reports only a rough byte-based token estimate; that estimate is not provider tokenization, billing, or an end-to-end measurement.

## Limitations and operations

- The local end-to-end benchmark uses real public GitHub PR data but synthetic signed webhook payloads. Live GitHub delivery and comment writes need webhook configuration and `GITHUB_TOKEN`.
- `GITHUB_TOKEN` and `OPENAI_API_KEY` are not needed to run tests or the public-PR benchmark; private repositories, comment publication, and LLM/semantic embedding calls need their respective credentials.
- Static rules are intentionally conservative and may produce false positives, as the measured sample did. Review all findings manually.
- GitHub file retrieval is capped; comments are not created if the existing-comment scan reaches its safety limit. Repeated deliveries older than Redis's 30-day deduplication TTL can be processed again.
- Redis uses AOF persistence, but production deployments still need backups, monitoring, secret management, and a GitHub App or similarly scoped credential.
- The Qdrant server and Python client are kept on the compatible 1.12 line. Back up Qdrant data and follow its upgrade guidance before changing either version.
- Retrieval and optional LLM review send changed patches and retrieved context only. Review the provider's data policy before using private repositories.

## Resume
# RepoPilot – AI-Augmented Code Review & Diagnostics Platform | Python, FastAPI, LangGraph, Qdrant, Docker, Redis, Celery
- Built a dual-agent LangGraph workflow featuring an Auditor to flag security and code-quality issues in PR diff lines, and a Diagnostician to analyze before/after performance profiles.
- Integrated Qdrant retrieval to surface indexed documentation during reviews, incorporating optional LLM-assisted analysis.
- Implemented an HMAC-verified FastAPI webhook and Redis-backed Celery workers to securely enqueue GitHub PR analysis asynchronously with duplicate-delivery handling.
- Verified the local webhook-to-result pipeline using 3 public PR diffs and locally generated, HMAC-signed events: 3/3 jobs succeeded, with a 1.07-second median turnaround in the test environment.
