"""Execution statistics for agent monitoring."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field


@dataclass
class PhaseStats:
    """Stats from one agent phase (classify, recheck, correlate, or summarize)."""

    turns: int = 0
    duration_ms: int = 0
    duration_api_ms: int = 0
    is_error: bool = False
    tool_calls: dict[str, int] = field(default_factory=dict)
    usage: dict | None = None

    def record_tool(self, name: str) -> None:
        self.tool_calls[name] = self.tool_calls.get(name, 0) + 1

    def to_dict(self) -> dict:
        d: dict = {
            "turns": self.turns,
            "duration_ms": self.duration_ms,
            "duration_api_ms": self.duration_api_ms,
            "is_error": self.is_error,
            "tool_calls": dict(self.tool_calls),
        }
        if self.usage:
            d["usage"] = self.usage
        return d


@dataclass
class AgentStats:
    """Combined stats for a classifier agent (both phases)."""

    run_id: str
    classify: PhaseStats = field(default_factory=PhaseStats)
    recheck: PhaseStats = field(default_factory=PhaseStats)


def distribution(values: list[int | float]) -> dict:
    """Compute min/max/avg/median for a list of numbers."""
    if not values:
        return {"min": 0, "max": 0, "avg": 0.0, "median": 0.0}
    return {
        "min": min(values),
        "max": max(values),
        "avg": round(sum(values) / len(values), 1),
        "median": round(statistics.median(values), 1),
    }


def _merge_tool_calls(stats_list: list[PhaseStats]) -> dict[str, int]:
    """Sum tool calls across a list of PhaseStats, sorted by frequency."""
    merged: dict[str, int] = {}
    for s in stats_list:
        for name, count in s.tool_calls.items():
            merged[name] = merged.get(name, 0) + count
    return dict(sorted(merged.items(), key=lambda x: -x[1]))


def build_classifier_summary(agents: list[AgentStats]) -> dict:
    """Aggregate classifier agent stats into a summary dict."""
    all_phases = [s for a in agents for s in (a.classify, a.recheck)]
    classify_turns = [a.classify.turns for a in agents if a.classify.turns]
    recheck_turns = [a.recheck.turns for a in agents if a.recheck.turns]
    classify_dur = [a.classify.duration_ms for a in agents if a.classify.duration_ms]
    recheck_dur = [a.recheck.duration_ms for a in agents if a.recheck.duration_ms]

    return {
        "count": len(agents),
        "errors": sum(
            1 for a in agents if a.classify.is_error or a.recheck.is_error
        ),
        "classify_phase": {
            "turns": distribution(classify_turns),
            "duration_ms": distribution(classify_dur),
        },
        "recheck_phase": {
            "turns": distribution(recheck_turns),
            "duration_ms": distribution(recheck_dur),
        },
        "tool_calls": _merge_tool_calls(all_phases),
    }


def build_execution_stats(
    classifier_agents: list[AgentStats],
    correlator: PhaseStats | None,
    summarizer: PhaseStats | None,
    model: str,
    version: str,
) -> dict:
    """Build the full execution_stats dict for report.json."""
    stats: dict = {
        "model": model,
        "flakectl_version": version,
        "classifier_agents": build_classifier_summary(classifier_agents),
    }
    if correlator:
        stats["correlator_agent"] = correlator.to_dict()
    if summarizer:
        stats["summarizer_agent"] = summarizer.to_dict()
    return stats
