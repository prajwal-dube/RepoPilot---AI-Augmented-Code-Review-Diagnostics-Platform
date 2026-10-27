"""Compare explicit before/after telemetry; no speculative leak claims."""
from .models import Finding


def diagnose(profiles: list[dict]) -> list[Finding]:
    findings = []
    for item in profiles:
        path = str(item.get("path", "runtime"))
        for metric, threshold, label in (("duration_ms", 1.5, "Latency regression"),
                                         ("peak_memory_mb", 1.5, "Peak-memory regression"),
                                         ("query_count", 2.0, "Query-count regression")):
            before = item.get("before", {}).get(metric)
            after = item.get("after", {}).get(metric)
            if (isinstance(before, (int, float)) and not isinstance(before, bool)
                    and isinstance(after, (int, float)) and not isinstance(after, bool)
                    and before > 0 and after / before >= threshold):
                findings.append(Finding("diagnostician", "medium", path, None,
                                        f"{label}: {before:g} -> {after:g} {metric} ({after / before:.2f}x)",
                                        "Measured before/after sample; confirm with repeated runs"))
    return findings
