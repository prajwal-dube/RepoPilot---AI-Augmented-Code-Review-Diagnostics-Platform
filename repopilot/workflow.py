"""Two-agent LangGraph with shared, bounded retrieved context."""
from typing import TypedDict
from .auditor import audit
from .diagnostician import diagnose
from .llm import review_with_llm
from .models import Review


class State(TypedDict):
    files: list[dict]
    profiles: list[dict]
    context: list[dict]
    auditor_findings: list
    diagnostic_findings: list


def auditor_node(state: State):
    return {"auditor_findings": audit(state["files"]) + review_with_llm(state["files"], state["context"])}


def diagnostician_node(state: State):
    return {"diagnostic_findings": diagnose(state["profiles"])}


def graph():
    from langgraph.graph import StateGraph, START, END
    builder = StateGraph(State)
    builder.add_node("auditor", auditor_node)
    builder.add_node("diagnostician", diagnostician_node)
    builder.add_edge(START, "auditor")
    builder.add_edge("auditor", "diagnostician")
    builder.add_edge("diagnostician", END)
    return builder.compile()


def analyze(repository: str, pull_number: int, files: list[dict], profiles=None, index=None, skipped=None):
    context = []
    if index and files:
        query = "\n".join(f["path"] + "\n" + (f.get("patch") or "")[:1500] for f in files[:15])[:12000]
        context = index.search(query, limit=3)
    state = graph().invoke({"files": files, "profiles": profiles or [], "context": context,
                            "auditor_findings": [], "diagnostic_findings": []})
    return Review(repository, pull_number,
                  state["auditor_findings"] + state["diagnostic_findings"],
                  skipped or [], [c["id"] for c in context])
