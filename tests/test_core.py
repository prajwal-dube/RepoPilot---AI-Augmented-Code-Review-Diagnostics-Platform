import unittest
from unittest.mock import patch
from repopilot.auditor import audit, added_lines
from repopilot.diagnostician import diagnose
from repopilot.benchmark import run, run_webhook


class CoreTests(unittest.TestCase):
    def test_added_lines_and_location(self):
        patch = "@@ -10,2 +10,3 @@\n old\n-removed\n+password = 'demo-secret'\n+new = 1"
        self.assertEqual(list(added_lines(patch)), [(11, "password = 'demo-secret'"), (12, "new = 1")])
        found = audit([{"path": "auth.py", "patch": patch}])
        self.assertEqual((found[0].line, found[0].severity), (11, "high"))

    def test_removed_line_not_flagged(self):
        self.assertEqual(audit([{"path": "x.py", "patch": "@@ -1 +1 @@\n-eval(data)\n+safe(data)"}]), [])

    def test_profiles_require_measured_regression(self):
        found = diagnose([{"path": "x.py", "before": {"duration_ms": 10}, "after": {"duration_ms": 16}}])
        self.assertEqual(len(found), 1)
        self.assertEqual(diagnose([{"before": {"duration_ms": 10}, "after": {"duration_ms": 12}}]), [])

    def test_benchmark_labels_scope(self):
        result = run(5)
        self.assertEqual(result["findings_per_sample"], 5)
        self.assertIn("local static", result["scope"])

    def test_webhook_benchmark_records_terminal_job_result(self):
        import hashlib
        import hmac
        import httpx

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def post(self, url, content, headers):
                expected = "sha256=" + hmac.new(b"secret", content, hashlib.sha256).hexdigest()
                self_signature = headers["X-Hub-Signature-256"]
                if url.endswith("/webhooks/github") and hmac.compare_digest(expected, self_signature):
                    return httpx.Response(202, json={"task_id": "job-1"},
                                          request=httpx.Request("POST", url))
                return httpx.Response(401, json={"detail": "invalid"})

            def get(self, url, headers):
                if "api.github.com" in url:
                    return httpx.Response(200, json={
                        "state": "open", "changed_files": 1, "additions": 1, "deletions": 0},
                        request=httpx.Request("GET", url))
                self.assert_admin_header(headers)
                return httpx.Response(200, json={
                    "state": "SUCCESS", "result": {"findings": []}, "error": None},
                    request=httpx.Request("GET", url))

            @staticmethod
            def assert_admin_header(headers):
                if headers["X-RepoPilot-Key"] != "admin":
                    raise AssertionError("Missing admin key")

        with patch("repopilot.benchmark.httpx.Client", return_value=FakeClient()):
            result = run_webhook("http://localhost:8000", "org/repo", [1], "secret", "admin")
        self.assertEqual(result["sample_size"], 1)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["records"][0]["diff"]["changed_files"], 1)
        self.assertEqual(result["records"][0]["result"], {"findings": []})
        self.assertIsNotNone(result["latency_seconds_median"])


if __name__ == "__main__":
    unittest.main()
