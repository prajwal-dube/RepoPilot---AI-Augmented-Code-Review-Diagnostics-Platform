"""Webhook is authenticated; manual endpoints need perimeter authentication in production."""
import hashlib
import hmac
import json
import logging
import os
import re
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from celery.result import AsyncResult
from .github import validate_repo
from .tasks import celery_app, review_task

app = FastAPI(title="RepoPilot", version="0.1.0")
logger = logging.getLogger(__name__)
MAX_WEBHOOK_BYTES = 1_000_000


def require_admin(x_repopilot_key: str | None = Header(default=None)):
    configured = os.getenv("REPOPILOT_ADMIN_KEY")
    if not configured:
        raise HTTPException(503, "Admin API key is not configured")
    if not x_repopilot_key or not hmac.compare_digest(configured, x_repopilot_key):
        raise HTTPException(401, "Invalid API key")


class ChangedFile(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    patch: str = Field(max_length=100000)


class AnalysisRequest(BaseModel):
    repository: str
    pull_number: int = Field(gt=0)
    files: list[ChangedFile] = Field(max_length=300)
    profiles: list[dict] = Field(default_factory=list, max_length=100)


class Document(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=12000)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyses", status_code=202, dependencies=[Depends(require_admin)])
def queue_analysis(body: AnalysisRequest):
    try:
        validate_repo(body.repository)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    job = review_task.delay(body.repository, body.pull_number, [f.model_dump() for f in body.files], body.profiles)
    return {"task_id": job.id}


@app.get("/analyses/{task_id}", dependencies=[Depends(require_admin)])
def get_analysis(task_id: str):
    if len(task_id) > 100 or not all(c.isalnum() or c in "-_" for c in task_id):
        raise HTTPException(422, "Invalid task ID")
    job = AsyncResult(task_id, app=celery_app)
    return {"state": job.state, "result": job.result if job.successful() else None,
            "error": str(job.result)[:200] if job.failed() else None}


@app.post("/context", status_code=201, dependencies=[Depends(require_admin)])
def add_context(body: Document):
    from .retrieval import ContextIndex
    try:
        ContextIndex().add(body.id, body.text)
    except Exception as exc:
        raise HTTPException(503, "Context index unavailable") from exc
    return {"id": body.id}


@app.post("/webhooks/github", status_code=202)
async def github_webhook(request: Request,
                         x_hub_signature_256: str | None = Header(default=None),
                         x_github_event: str | None = Header(default=None),
                         x_github_delivery: str | None = Header(default=None)):
    secret = os.getenv("GITHUB_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(503, "Webhook secret is not configured")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_WEBHOOK_BYTES:
                raise HTTPException(413, "Payload too large")
        except ValueError as exc:
            raise HTTPException(400, "Invalid content length") from exc
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "Payload too large")
    raw = bytes(chunks)
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise HTTPException(413, "Payload too large")
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not x_hub_signature_256 or not hmac.compare_digest(signature, x_hub_signature_256):
        raise HTTPException(401, "Invalid webhook signature")
    if x_github_event != "pull_request":
        return {"ignored": True}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        if data.get("action") not in {"opened", "synchronize", "reopened", "ready_for_review"}:
            return {"ignored": True}
        repository = data["repository"]
        number = data["number"]
        if not isinstance(repository, dict) or isinstance(number, bool) or not isinstance(number, int):
            raise ValueError()
        repo = validate_repo(repository["full_name"])
        if not 0 < number < 1_000_000_000:
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, "Malformed pull request event") from exc
    if not x_github_delivery or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", x_github_delivery):
        raise HTTPException(422, "Missing delivery ID")
    import redis
    connection = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    key = "repopilot:delivery:" + x_github_delivery
    try:
        if not connection.set(key, "queued", nx=True, ex=30 * 86400):
            return {"duplicate": True}
        try:
            job = review_task.delay(repo, number, publish=True)
        except Exception:
            logger.exception("Failed to enqueue GitHub delivery %s", x_github_delivery)
            try:
                connection.delete(key)
            except redis.RedisError:
                logger.exception("Failed to release GitHub delivery reservation %s", x_github_delivery)
            raise
    except redis.RedisError as exc:
        raise HTTPException(503, "Queue unavailable") from exc
    finally:
        connection.close()
    return {"task_id": job.id}
