"""Reproducible local microbenchmark; excludes network and provider billing."""
import argparse
import hashlib
import hmac
import json
import math
import os
import platform
import statistics
import time
import uuid
from pathlib import Path
import httpx
from .auditor import audit
from .diagnostician import diagnose


def estimate_tokens(text):
    # Deliberately labeled approximation, not provider usage data.
    return (len(text.encode()) + 3) // 4


def run(repeat=30):
    files = [{"path": "service.py", "patch": "@@ -1,0 +1,3 @@\n+password = 'demo-secret'\n+result = eval(payload)\n+requests.get(url)"}]
    profiles = [{"path": "service.py", "before": {"duration_ms": 10, "peak_memory_mb": 20},
                 "after": {"duration_ms": 18, "peak_memory_mb": 35}}]
    corpus = ["Auth: use short-lived tokens, never embed secrets in source.",
              "Data: use parameterized queries and validate inputs.",
              "Runtime: latency baseline 10 ms, peak memory baseline 20 MB."]
    retrieved = [corpus[0], corpus[2]]
    elapsed = []
    findings = 0
    for _ in range(repeat):
        start = time.perf_counter()
        findings = len(audit(files) + diagnose(profiles))
        elapsed.append((time.perf_counter() - start) * 1000)
    full = estimate_tokens("\n".join(corpus))
    targeted = estimate_tokens("\n".join(retrieved))
    return {"scope": "local static checks only; no API, queue, GitHub, Qdrant, LLM, or network",
            "sample_count": repeat, "findings_per_sample": findings,
            "latency_ms_median": round(statistics.median(elapsed), 4),
            "latency_ms_p95": round(sorted(elapsed)[max(0, int(repeat * .95 + .9999) - 1)], 4),
            "estimated_context_tokens_full": full, "estimated_context_tokens_retrieved": targeted,
            "estimated_context_reduction_percent": round(100 * (full - targeted) / full, 2)}


def run_webhook(api_url, repository, pull_numbers, secret, admin_key, timeout=300, poll_interval=0.5):
    records = []
    base_url = api_url.rstrip("/")
    headers = {"X-RepoPilot-Key": admin_key}
    github_headers = {"Accept": "application/vnd.github+json",
                      "X-GitHub-Api-Version": "2022-11-28"}
    if os.getenv("GITHUB_TOKEN"):
        github_headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    with httpx.Client(timeout=30) as client:
        for pull_number in pull_numbers:
            payload = {"action": "synchronize",
                       "number": pull_number,
                       "repository": {"full_name": repository}}
            raw = json.dumps(payload, separators=(",", ":")).encode()
            delivery_id = str(uuid.uuid4())
            signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            record = {"pull_number": pull_number, "delivery_id": delivery_id}
            try:
                metadata_response = client.get(
                    f"https://api.github.com/repos/{repository}/pulls/{pull_number}",
                    headers=github_headers)
                metadata_response.raise_for_status()
                metadata = metadata_response.json()
                record["diff"] = {
                    "state": metadata.get("state"),
                    "changed_files": metadata.get("changed_files"),
                    "additions": metadata.get("additions"),
                    "deletions": metadata.get("deletions"),
                }
            except (httpx.HTTPError, ValueError) as exc:
                record["diff_metadata_error"] = str(exc)[:300]
            started = time.perf_counter()
            try:
                response = client.post(
                    base_url + "/webhooks/github",
                    content=raw,
                    headers={"X-GitHub-Event": "pull_request",
                             "X-GitHub-Delivery": delivery_id,
                             "X-Hub-Signature-256": signature})
                record["webhook_status"] = response.status_code
                webhook_result = response.json()
                if response.status_code != 202 or "task_id" not in webhook_result:
                    record.update({"state": "webhook_rejected", "response": webhook_result})
                else:
                    task_id = webhook_result["task_id"]
                    record["task_id"] = task_id
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        status = client.get(f"{base_url}/analyses/{task_id}", headers=headers)
                        status.raise_for_status()
                        body = status.json()
                        record["state"] = body["state"]
                        if body["state"] in {"SUCCESS", "FAILURE", "REVOKED"}:
                            record["result"] = body.get("result")
                            record["error"] = body.get("error")
                            break
                        time.sleep(poll_interval)
                    else:
                        record.update({"state": "timeout", "error": f"No terminal state within {timeout}s"})
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                record.update({"state": "benchmark_error", "error": str(exc)[:300]})
            record["elapsed_seconds"] = round(time.perf_counter() - started, 4)
            records.append(record)

    successful = [record["elapsed_seconds"] for record in records if record.get("state") == "SUCCESS"]
    ordered = sorted(successful)
    return {
        "scope": "signed webhook to terminal Celery result through the configured API, worker, GitHub API, and retrieval service",
        "repository": repository,
        "sample_size": len(records),
        "success_count": len(successful),
        "failure_count": len(records) - len(successful),
        "latency_seconds_median": round(statistics.median(successful), 4) if successful else None,
        "latency_seconds_p95": round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 4) if ordered else None,
        "environment": {
            "api_url": base_url,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "github_token_configured": bool(os.getenv("GITHUB_TOKEN")),
            "openai_api_key_configured": bool(os.getenv("OPENAI_API_KEY")),
            "llm_token_usage_and_cost": (
                "not measured: OPENAI_API_KEY is not configured"
                if not os.getenv("OPENAI_API_KEY")
                else "not instrumented in the worker result"),
        },
        "records": records,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=30)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--repository", help="Repository to benchmark with signed PR webhook events")
    parser.add_argument("--pulls", type=int, nargs="+", help="Representative pull request numbers")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 100000:
        parser.error("repeat must be between 1 and 100000")
    if args.repository or args.pulls:
        if not args.repository or not args.pulls:
            parser.error("--repository and --pulls must be supplied together")
        if args.timeout < 1 or args.poll_interval <= 0:
            parser.error("--timeout and --poll-interval must be positive")
        secret = os.getenv("GITHUB_WEBHOOK_SECRET")
        admin_key = os.getenv("REPOPILOT_ADMIN_KEY")
        if not secret or not admin_key:
            parser.error("GITHUB_WEBHOOK_SECRET and REPOPILOT_ADMIN_KEY must be set")
        result = run_webhook(args.api_url, args.repository, args.pulls, secret,
                             admin_key, args.timeout, args.poll_interval)
    else:
        result = run(args.repeat)
    output = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
