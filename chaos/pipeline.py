"""Glue shared by the API and the CLI: analyse + export a run, summarise
findings and scores, and render the final report from persisted results."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import catalog, scoring
from .analyst import analyze, ranked_findings
from .discovery import discover
from .evidence import export_run
from .llm import Provider
from .report import format_findings_report
from .storage import Store


def analyze_and_export(store: Store, run_id: str, graph: dict, provider: Optional[Provider],
                       export_dir: Path | None = None) -> dict:
    finding = analyze(store, run_id, graph, provider)
    export_run(store, run_id, export_dir)
    return finding


def analyzable_runs(store: Store, limit: int = 200) -> list[str]:
    """Finished experiment runs, oldest first (rejected runs have no evidence)."""
    runs = [r for r in store.list_runs("experiment", limit) if r["state"] != "rejected" and r.get("ended")]
    return [r["id"] for r in reversed(runs)]


def findings_summary(store: Store) -> dict:
    by_age = sorted(store.list_analyses(), key=lambda f: f.get("run_created") or 0)
    return {"overall": scoring.overall(by_age, catalog.scenarios()),
            "findings": ranked_findings(store),
            "methodology": (scoring.__doc__ or "").strip()}


def full_report(store: Store, docker=None) -> str:
    summary = findings_summary(store)
    graph = discover(docker) if docker is not None else None
    runs = {f["run_id"]: store.get_run(f["run_id"]) or {} for f in summary["findings"]}
    return format_findings_report(store.latest_plan(), summary["findings"], summary["overall"], runs, graph)
