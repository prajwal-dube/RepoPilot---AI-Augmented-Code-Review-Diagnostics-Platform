from dataclasses import dataclass, field, asdict


@dataclass
class Finding:
    agent: str
    severity: str
    path: str
    line: int | None
    message: str
    evidence: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class Review:
    repository: str
    pull_number: int
    findings: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    context_ids: list[str] = field(default_factory=list)

    def to_dict(self):
        return {"repository": self.repository, "pull_number": self.pull_number,
                "findings": [f.to_dict() for f in self.findings],
                "skipped": self.skipped, "context_ids": self.context_ids}
