import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from repopilot.api import app
from repopilot.github import GitHub, RetryableGitHubError, format_review
from repopilot.models import Finding, Review
from repopilot.retrieval import ContextIndex
from repopilot.tasks import review_task
from repopilot.workflow import analyze


class IntegrationTests(unittest.TestCase):
    def test_qdrant_and_two_agent_workflow(self):
        index = ContextIndex(client=QdrantClient(":memory:"), embeddings="lexical")
        index.add("auth-guidance", "password credentials should never be in source code")
        files = [{"path": "auth.py", "patch": "@@ -0,0 +1 @@\n+password = 'example-secret'"}]
        profiles = [{"path": "auth.py", "before": {"duration_ms": 10}, "after": {"duration_ms": 20}}]
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            result = analyze("team/repo", 7, files, profiles, index=index)
        self.assertEqual(len(result.findings), 2)
        self.assertEqual({f.agent for f in result.findings}, {"auditor", "diagnostician"})
        self.assertEqual(result.context_ids, ["auth-guidance"])

    def test_webhook_signature_and_enqueue(self):
        payload = {"action": "opened", "repository": {"full_name": "team/repo"}, "number": 7}
        raw = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        client = TestClient(app)
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "test-secret"}):
            bad = client.post("/webhooks/github", content=raw, headers={"X-GitHub-Event": "pull_request"})
            self.assertEqual(bad.status_code, 401)
            with patch("redis.Redis.from_url") as redis, patch("repopilot.api.review_task.delay") as delay:
                redis.return_value.set.return_value = True
                delay.return_value.id = "task-7"
                response = client.post("/webhooks/github", content=raw, headers={
                    "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "delivery-7",
                    "X-Hub-Signature-256": signature})
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json(), {"task_id": "task-7"})
                delay.assert_called_once_with("team/repo", 7, publish=True)
                redis.return_value.set.assert_called_once_with(
                    "repopilot:delivery:delivery-7", "queued", nx=True, ex=30 * 86400)

    def test_duplicate_webhook_delivery_is_not_enqueued_twice(self):
        payload = {"action": "opened", "repository": {"full_name": "team/repo"}, "number": 7}
        raw = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        headers = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "delivery-7",
                   "X-Hub-Signature-256": signature}
        client = TestClient(app)
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "test-secret"}):
            with patch("redis.Redis.from_url") as redis, patch("repopilot.api.review_task.delay") as delay:
                redis.return_value.set.side_effect = [True, False]
                delay.return_value.id = "task-7"
                first = client.post("/webhooks/github", content=raw, headers=headers)
                second = client.post("/webhooks/github", content=raw, headers=headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.json(), {"duplicate": True})
        delay.assert_called_once_with("team/repo", 7, publish=True)

    def test_manual_endpoints_fail_closed_without_admin_key(self):
        client = TestClient(app)
        with patch.dict(os.environ, {"REPOPILOT_ADMIN_KEY": ""}):
            queued = client.post("/analyses", json={"repository": "team/repo", "pull_number": 1, "files": []})
            status = client.get("/analyses/task-1")
            context = client.post("/context", json={"id": "doc", "text": "guidance"})
        self.assertEqual(queued.status_code, 503)
        self.assertEqual(status.status_code, 503)
        self.assertEqual(context.status_code, 503)

    def test_manual_endpoints_require_configured_key(self):
        client = TestClient(app)
        request_body = {"repository": "team/repo", "pull_number": 1, "files": []}
        with patch.dict(os.environ, {"REPOPILOT_ADMIN_KEY": "expected"}):
            denied = client.post("/analyses", json=request_body, headers={"X-RepoPilot-Key": "wrong"})
            with patch("repopilot.api.review_task.delay") as delay:
                delay.return_value.id = "task-1"
                accepted = client.post("/analyses", json=request_body,
                                       headers={"X-RepoPilot-Key": "expected"})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(accepted.status_code, 202)

    def test_signed_malformed_webhook_is_rejected_without_enqueue(self):
        client = TestClient(app)
        payload = {"action": "opened", "repository": {"full_name": "team/repo"}, "number": 1.5}
        raw = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "test-secret"}):
            with patch("repopilot.api.review_task.delay") as delay:
                response = client.post("/webhooks/github", content=raw, headers={
                    "X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "delivery-8",
                    "X-Hub-Signature-256": signature})
        self.assertEqual(response.status_code, 422)
        delay.assert_not_called()

    def test_webhook_rejects_oversized_body_before_enqueue(self):
        client = TestClient(app)
        with patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "test-secret"}):
            with patch("repopilot.api.review_task.delay") as delay:
                response = client.post("/webhooks/github", content=b"x" * 1_000_001)
        self.assertEqual(response.status_code, 413)
        delay.assert_not_called()

    def test_github_file_api_without_network(self):
        import httpx
        requests = []

        def handler(request):
            requests.append(str(request.url))
            self.assertEqual(request.headers["Authorization"], "Bearer demo")
            return httpx.Response(200, json=[{"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+eval(x)"},
                                             {"filename": "binary.png"}])

        client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
        files, skipped = GitHub(token="demo", client=client).files("team/repo", 1)
        self.assertEqual(files[0]["path"], "app.py")
        self.assertIn("binary.png", skipped[0])
        self.assertEqual(len(requests), 1)

    def test_github_file_pagination_follows_next_link(self):
        import httpx
        requests = []

        def handler(request):
            requests.append(request.url)
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json=[{"filename": "second.py", "patch": "+pass"}])
            return httpx.Response(200, json=[{"filename": "first.py", "patch": "+pass"}],
                                  headers={"Link": '<https://api.github.com/repos/team/repo/pulls/1/files?per_page=100&page=2>; rel="next"'})

        client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
        files, skipped = GitHub(token="demo", client=client).files("team/repo", 1)
        self.assertEqual([item["path"] for item in files], ["first.py", "second.py"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(skipped, [])

    def test_github_rejects_external_pagination_link(self):
        import httpx

        def handler(_request):
            return httpx.Response(200, json=[], headers={"Link": '<https://attacker.example/leak>; rel="next"'})

        client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
        with self.assertRaisesRegex(ValueError, "unexpected pagination URL"):
            GitHub(token="demo", client=client).files("team/repo", 1)

    def test_github_reuses_existing_bot_comment_after_fourth_page(self):
        import httpx
        requests = []

        def handler(request):
            requests.append(request)
            page = int(request.url.params.get("page", "1"))
            if request.method == "PATCH":
                return httpx.Response(200, json={"id": 1})
            if page < 4:
                return httpx.Response(200, json=[{"body": "other", "user": {"type": "User"}}] * 100,
                                      headers={"Link": f'<https://api.github.com/repos/team/repo/issues/1/comments?per_page=100&page={page + 1}>; rel="next"'})
            return httpx.Response(200, json=[{"id": 42, "body": "<!-- repopilot-review -->",
                                              "user": {"type": "Bot"}}])

        client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
        result = GitHub(token="demo", client=client).comment("team/repo", 1, "updated review")
        self.assertTrue(result)
        self.assertEqual(sum(request.method == "GET" for request in requests), 4)
        self.assertEqual([request.method for request in requests[-1:]], ["PATCH"])

    def test_github_does_not_post_if_existing_comment_scan_is_incomplete(self):
        import httpx

        def handler(request):
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(
                200, json=[{"body": "other", "user": {"type": "User"}}] * 100,
                headers={"Link": f'<https://api.github.com/repos/team/repo/issues/1/comments?per_page=100&page={page + 1}>; rel="next"'})

        client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
        with self.assertRaisesRegex(RuntimeError, "pagination limit"):
            GitHub(token="demo", client=client).comment("team/repo", 1, "review")

    def test_github_retries_transient_responses_only(self):
        import httpx
        client = httpx.Client(base_url="https://api.github.com",
                              transport=httpx.MockTransport(lambda _: httpx.Response(503)))
        with self.assertRaises(RetryableGitHubError):
            GitHub(client=client).files("team/repo", 1)
        limited = httpx.Client(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})))
        with self.assertRaises(RetryableGitHubError):
            GitHub(client=limited).files("team/repo", 1)
        forbidden = httpx.Client(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(lambda _: httpx.Response(403)))
        with self.assertRaises(httpx.HTTPStatusError):
            GitHub(client=forbidden).files("team/repo", 1)
        self.assertEqual(review_task.max_retries, 3)

    def test_task_serializes_comment_update_per_pull_request(self):
        from unittest.mock import MagicMock

        github = MagicMock()
        github.token = "test-token"
        lock = MagicMock()
        lock.acquire.return_value = True
        connection = MagicMock()
        connection.lock.return_value = lock
        review = Review("team/repo", 4)
        with patch("repopilot.tasks.GitHub", return_value=github), \
             patch("repopilot.tasks.analyze", return_value=review), \
             patch("repopilot.retrieval.ContextIndex"), \
             patch("redis.Redis.from_url", return_value=connection):
            result = review_task.run("team/repo", 4, [], None, True)
        self.assertEqual(result["pull_number"], 4)
        connection.lock.assert_called_once_with(
            "repopilot:comment:team/repo:4", timeout=600, blocking_timeout=60)
        lock.acquire.assert_called_once_with(blocking=True)
        lock.release.assert_called_once_with()
        github.comment.assert_called_once()
        connection.close.assert_called_once_with()

    def test_review_comment_escapes_mentions_from_untrusted_content(self):
        review = Review("team/repo", 1, [Finding("auditor-llm", "high", "file.py", 1,
                                                "notify @maintainer", "evidence")])
        self.assertIn("@\u200bmaintainer", format_review(review))
        self.assertNotIn("notify @maintainer", format_review(review))


if __name__ == "__main__":
    unittest.main()
