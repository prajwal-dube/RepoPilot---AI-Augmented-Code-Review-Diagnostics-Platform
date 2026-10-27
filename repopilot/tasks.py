import os
import httpx
import redis
from celery import Celery
from .github import GitHub, RetryableGitHubError, format_review
from .workflow import analyze

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
celery_app = Celery("repopilot", broker=redis_url, backend=redis_url)
celery_app.conf.update(task_track_started=True, task_serializer="json", accept_content=["json"],
                       result_serializer="json", broker_connection_retry_on_startup=True)


@celery_app.task(
    name="repopilot.review",
    bind=True,
    autoretry_for=(httpx.RequestError, RetryableGitHubError, redis.RedisError),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def review_task(self, repository: str, pull_number: int, files=None, profiles=None, publish=False):
    github = GitHub()
    skipped = []
    if files is None:
        files, skipped = github.files(repository, pull_number)
    index = None
    try:
        from .retrieval import ContextIndex
        index = ContextIndex()
    except Exception as exc:
        # Static checks remain available during an index outage; surface the outage.
        skipped.append(f"Context retrieval unavailable: {type(exc).__name__}")
    result = analyze(repository, pull_number, files, profiles, index=index, skipped=skipped)
    if publish:
        if github.token:
            connection = redis.Redis.from_url(redis_url)
            lock = connection.lock(
                f"repopilot:comment:{repository}:{pull_number}",
                timeout=600,
                blocking_timeout=60,
            )
            acquired = False
            try:
                acquired = lock.acquire(blocking=True)
                if not acquired:
                    raise RetryableGitHubError("Could not acquire the pull request comment lock")
                github.comment(repository, pull_number, format_review(result))
            finally:
                if acquired:
                    lock.release()
                connection.close()
    return result.to_dict()
