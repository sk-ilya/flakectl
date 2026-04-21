"""Tests for flakectl.stats -- execution statistics."""

from flakectl.stats import (
    AgentStats,
    PhaseStats,
    _merge_tool_calls,
    build_classifier_summary,
    build_execution_stats,
    distribution,
)

# ---------------------------------------------------------------------------
# PhaseStats
# ---------------------------------------------------------------------------

class TestPhaseStats:
    def test_record_tool_accumulates(self):
        s = PhaseStats()
        s.record_tool("Read")
        s.record_tool("Read")
        s.record_tool("Grep")
        assert s.tool_calls == {"Read": 2, "Grep": 1}

    def test_to_dict_basic(self):
        s = PhaseStats(turns=5, duration_ms=1000, duration_api_ms=800, is_error=False)
        s.record_tool("Read")
        d = s.to_dict()
        assert d["turns"] == 5
        assert d["duration_ms"] == 1000
        assert d["duration_api_ms"] == 800
        assert d["is_error"] is False
        assert d["tool_calls"] == {"Read": 1}
        assert "usage" not in d

    def test_to_dict_includes_usage_when_set(self):
        s = PhaseStats(usage={"input_tokens": 100, "output_tokens": 50})
        d = s.to_dict()
        assert d["usage"] == {"input_tokens": 100, "output_tokens": 50}

    def test_to_dict_excludes_usage_when_none(self):
        s = PhaseStats()
        d = s.to_dict()
        assert "usage" not in d

    def test_defaults(self):
        s = PhaseStats()
        assert s.turns == 0
        assert s.duration_ms == 0
        assert s.is_error is False
        assert s.tool_calls == {}
        assert s.usage is None


# ---------------------------------------------------------------------------
# distribution
# ---------------------------------------------------------------------------

class TestDistribution:
    def test_empty_list(self):
        result = distribution([])
        assert result == {"min": 0, "max": 0, "avg": 0.0, "median": 0.0}

    def test_single_value(self):
        result = distribution([42])
        assert result == {"min": 42, "max": 42, "avg": 42.0, "median": 42.0}

    def test_normal_list(self):
        result = distribution([10, 20, 30, 40])
        assert result["min"] == 10
        assert result["max"] == 40
        assert result["avg"] == 25.0
        assert result["median"] == 25.0

    def test_odd_count(self):
        result = distribution([1, 2, 3])
        assert result["median"] == 2.0

    def test_all_same(self):
        result = distribution([5, 5, 5])
        assert result == {"min": 5, "max": 5, "avg": 5.0, "median": 5.0}

    def test_floats(self):
        result = distribution([1.5, 2.5, 3.5])
        assert result["avg"] == 2.5
        assert result["median"] == 2.5


# ---------------------------------------------------------------------------
# _merge_tool_calls
# ---------------------------------------------------------------------------

class TestMergeToolCalls:
    def test_merges_across_phases(self):
        s1 = PhaseStats(tool_calls={"Read": 3, "Grep": 1})
        s2 = PhaseStats(tool_calls={"Read": 2, "Edit": 5})
        result = _merge_tool_calls([s1, s2])
        assert result == {"Edit": 5, "Read": 5, "Grep": 1}

    def test_sorted_by_frequency(self):
        s1 = PhaseStats(tool_calls={"A": 1, "B": 10, "C": 5})
        result = _merge_tool_calls([s1])
        assert list(result.keys()) == ["B", "C", "A"]

    def test_empty_list(self):
        assert _merge_tool_calls([]) == {}

    def test_empty_tool_calls(self):
        result = _merge_tool_calls([PhaseStats()])
        assert result == {}


# ---------------------------------------------------------------------------
# build_classifier_summary
# ---------------------------------------------------------------------------

class TestBuildClassifierSummary:
    def test_basic_summary(self):
        agents = [
            AgentStats(
                run_id="1",
                classify=PhaseStats(turns=10, duration_ms=5000),
                recheck=PhaseStats(turns=3, duration_ms=2000),
            ),
            AgentStats(
                run_id="2",
                classify=PhaseStats(turns=20, duration_ms=10000),
                recheck=PhaseStats(turns=5, duration_ms=3000),
            ),
        ]
        result = build_classifier_summary(agents)
        assert result["count"] == 2
        assert result["errors"] == 0
        assert result["classify_phase"]["turns"]["min"] == 10
        assert result["classify_phase"]["turns"]["max"] == 20
        assert result["recheck_phase"]["turns"]["min"] == 3
        assert result["recheck_phase"]["turns"]["max"] == 5

    def test_errors_counted(self):
        agents = [
            AgentStats(
                run_id="1",
                classify=PhaseStats(is_error=True),
            ),
            AgentStats(
                run_id="2",
                classify=PhaseStats(is_error=False),
                recheck=PhaseStats(is_error=True),
            ),
            AgentStats(run_id="3"),
        ]
        result = build_classifier_summary(agents)
        assert result["errors"] == 2

    def test_tool_calls_merged(self):
        agents = [
            AgentStats(
                run_id="1",
                classify=PhaseStats(tool_calls={"Read": 5}),
                recheck=PhaseStats(tool_calls={"Read": 2, "Edit": 1}),
            ),
            AgentStats(
                run_id="2",
                classify=PhaseStats(tool_calls={"Read": 3, "Grep": 4}),
            ),
        ]
        result = build_classifier_summary(agents)
        assert result["tool_calls"]["Read"] == 10
        assert result["tool_calls"]["Grep"] == 4
        assert result["tool_calls"]["Edit"] == 1

    def test_empty_agents(self):
        result = build_classifier_summary([])
        assert result["count"] == 0
        assert result["errors"] == 0

    def test_skips_zero_turns_in_distribution(self):
        agents = [
            AgentStats(
                run_id="1",
                classify=PhaseStats(turns=10),
                recheck=PhaseStats(turns=0),
            ),
        ]
        result = build_classifier_summary(agents)
        assert result["classify_phase"]["turns"]["min"] == 10
        assert result["recheck_phase"]["turns"] == {
            "min": 0, "max": 0, "avg": 0.0, "median": 0.0,
        }


# ---------------------------------------------------------------------------
# build_execution_stats
# ---------------------------------------------------------------------------

class TestBuildExecutionStats:
    def test_full_assembly(self):
        agents = [AgentStats(run_id="1", classify=PhaseStats(turns=10))]
        correlator = PhaseStats(turns=20, duration_ms=5000)
        summarizer = PhaseStats(turns=3, duration_ms=1000)

        result = build_execution_stats(
            classifier_agents=agents,
            correlator=correlator,
            summarizer=summarizer,
            model="sonnet",
            version="0.1.0",
        )
        assert result["model"] == "sonnet"
        assert result["flakectl_version"] == "0.1.0"
        assert result["classifier_agents"]["count"] == 1
        assert result["correlator_agent"]["turns"] == 20
        assert result["summarizer_agent"]["turns"] == 3

    def test_without_correlator(self):
        result = build_execution_stats(
            classifier_agents=[],
            correlator=None,
            summarizer=PhaseStats(turns=2),
            model="haiku",
            version="0.2.0",
        )
        assert "correlator_agent" not in result
        assert "summarizer_agent" in result

    def test_without_summarizer(self):
        result = build_execution_stats(
            classifier_agents=[],
            correlator=PhaseStats(turns=5),
            summarizer=None,
            model="opus",
            version="0.1.0",
        )
        assert "correlator_agent" in result
        assert "summarizer_agent" not in result
