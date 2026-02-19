"""Tests for flakectl.cli -- simple helpers."""

from flakectl.cli import _parse_csv_list, _resolve_context

# ---------------------------------------------------------------------------
# _parse_csv_list
# ---------------------------------------------------------------------------

class TestParseCsvList:
    def test_basic_split(self):
        assert _parse_csv_list("a,b,c") == ["a", "b", "c"]

    def test_spaces_trimmed(self):
        assert _parse_csv_list("a , b , c") == ["a", "b", "c"]

    def test_empty_string(self):
        assert _parse_csv_list("") == []

    def test_trailing_comma(self):
        assert _parse_csv_list("a,b,") == ["a", "b"]


# ---------------------------------------------------------------------------
# _resolve_context
# ---------------------------------------------------------------------------

class TestResolveContext:
    def test_inline_text_returned(self):
        assert _resolve_context("some inline text") == "some inline text"

    def test_at_path_reads_file(self, tmp_path):
        ctx_file = tmp_path / "context.txt"
        ctx_file.write_text("file content here")
        result = _resolve_context(f"@{ctx_file}")
        assert result == "file content here"

    def test_empty_string(self):
        assert _resolve_context("") == ""
