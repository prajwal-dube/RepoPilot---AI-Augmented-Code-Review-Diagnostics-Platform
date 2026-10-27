"""Conservative checks on added diff lines; never execute PR code."""
import re
from .models import Finding

RULES = [
    (re.compile(r"\b(?:password|api_key|secret|token)\s*=\s*['\"][^'\"]{3,}['\"]", re.I),
     "high", "Possible hard-coded credential"),
    (re.compile(r"\b(?:eval|exec)\s*\("), "high", "Dynamic execution of input; inspect trust boundary"),
    (re.compile(r"\bshell\s*=\s*True\b"), "high", "Shell execution may allow command injection"),
    (re.compile(r"\b(?:verify\s*=\s*False|CERT_NONE)\b"), "high", "TLS verification disabled"),
    (re.compile(r"\b(?:SELECT|UPDATE|DELETE|INSERT)\b.*(?:\{[^}]+\}|%s|\+\s*\w+)", re.I),
     "medium", "Possible dynamically constructed SQL; check parameterization"),
    (re.compile(r"\b(?:time\.sleep|requests\.(?:get|post))\s*\("),
     "low", "Potential blocking call in an async execution path"),
]


def added_lines(patch: str):
    new_line = 0
    for raw in patch.splitlines():
        match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if match:
            new_line = int(match.group(1))
        elif raw.startswith("+") and not raw.startswith("+++"):
            yield new_line, raw[1:]
            new_line += 1
        elif raw.startswith(" "):
            new_line += 1
        # Removed lines do not advance the new-file line number.


def audit(files: list[dict]) -> list[Finding]:
    findings = []
    for file in files:
        path = file["path"]
        for line, source in added_lines(file.get("patch") or ""):
            for pattern, severity, message in RULES:
                if pattern.search(source):
                    findings.append(Finding("auditor", severity, path, line, message,
                                            source.strip()[:180]))
    return findings
