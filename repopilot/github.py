"""GitHub API adapter with explicit host and bounded pagination."""
import os
import re
import httpx

MAX_FILES = 300
MAX_PATCH_BYTES = 450_000
MAX_COMMENT_PAGES = 20


class RetryableGitHubError(RuntimeError):
    """A GitHub response that may succeed after retrying."""


def validate_repo(repo: str):
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or ".." in repo:
        raise ValueError("Invalid owner/repository")
    return repo


class GitHub:
    def __init__(self, token=None, client=None):
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN", "")
        self.client = client or httpx.Client(base_url="https://api.github.com", timeout=20,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                     **({"Authorization": f"Bearer {self.token}"} if self.token else {})})

        if self.token:
            self.client.headers["Authorization"] = f"Bearer {self.token}"

    def _request(self, method, url, **kwargs):
        response = self.client.request(method, url, **kwargs)
        rate_limited = response.status_code == 403 and (
            response.headers.get("X-RateLimit-Remaining") == "0" or "Retry-After" in response.headers
        )
        if response.status_code == 429 or response.status_code >= 500 or rate_limited:
            raise RetryableGitHubError(f"GitHub returned retryable HTTP {response.status_code}")
        response.raise_for_status()
        return response

    @staticmethod
    def _next_page(response):
        next_url = response.links.get("next", {}).get("url")
        if not next_url:
            return None
        url = httpx.URL(next_url)
        if url.scheme != "https" or url.host != "api.github.com":
            raise ValueError("GitHub returned an unexpected pagination URL")
        return next_url

    def files(self, repo, number):
        validate_repo(repo)
        if not 0 < number < 1_000_000_000:
            raise ValueError("Invalid PR number")
        files, skipped, size, processed = [], [], 0, 0
        next_url = None
        for page in range(1, 4):
            if next_url:
                response = self._request("GET", next_url)
            else:
                response = self._request(
                    "GET", f"/repos/{repo}/pulls/{number}/files",
                    params={"per_page": 100, "page": page})
            batch = response.json()
            if not isinstance(batch, list):
                raise ValueError("GitHub returned an invalid pull request file list")
            for entry in batch:
                if processed >= MAX_FILES:
                    break
                processed += 1
                path, patch = entry["filename"], entry.get("patch")
                if not patch:
                    skipped.append(path + " (binary or patch unavailable)")
                    continue
                byte_count = len(patch.encode())
                if size + byte_count > MAX_PATCH_BYTES:
                    skipped.append(path + " (diff size limit)")
                    continue
                files.append({"path": path, "patch": patch})
                size += byte_count
            next_url = self._next_page(response)
            if not next_url:
                break
            if processed >= MAX_FILES:
                skipped.append("Additional files omitted after 300-file GitHub API limit")
                break
        if next_url and page == 3:
            skipped.append("Additional files omitted after 300-file GitHub API limit")
        return files, skipped

    def comment(self, repo, number, markdown):
        if not self.token:
            return False
        validate_repo(repo)
        marker = "<!-- repopilot-review -->"
        url = f"/repos/{repo}/issues/{number}/comments"
        next_url = None
        for page in range(1, MAX_COMMENT_PAGES + 1):
            if next_url:
                response = self._request("GET", next_url)
            else:
                response = self._request("GET", url, params={"per_page": 100, "page": page})
            comments = response.json()
            if not isinstance(comments, list):
                raise ValueError("GitHub returned an invalid pull request comment list")
            for comment in comments:
                if marker in comment.get("body", "") and comment.get("user", {}).get("type") == "Bot":
                    self._request("PATCH", f"/repos/{repo}/issues/comments/{comment['id']}",
                                  json={"body": markdown})
                    return True
            next_url = self._next_page(response)
            if not next_url:
                break
        else:
            raise RuntimeError("Could not verify the existing RepoPilot comment within the pagination limit")
        self._request("POST", url, json={"body": markdown})
        return True


def format_review(review):
    def safe(value):
        return str(value).replace("@", "@\u200b").replace("`", "'").replace("\n", " ").replace("\r", " ")[:300]

    rows = ["<!-- repopilot-review -->", "## RepoPilot review", "", "Automated suggestions; verify before changing code.", ""]
    if not review.findings:
        rows.append("No findings from configured checks. This is not a security guarantee.")
    for f in review.findings[:25]:
        location = f"`{safe(f.path)}:{f.line}`" if f.line else f"`{safe(f.path)}`"
        rows.append(f"- **{f.severity.upper()}** {location}: {safe(f.message)} ({safe(f.agent)})")
    if review.context_ids:
        rows.extend(["", "Context: " + ", ".join(f"`{safe(x)}`" for x in review.context_ids)])
    if review.skipped:
        rows.extend(["", "Skipped: " + ", ".join(f"`{safe(x)}`" for x in review.skipped[:20])])
    return "\n".join(rows)[:60000]
