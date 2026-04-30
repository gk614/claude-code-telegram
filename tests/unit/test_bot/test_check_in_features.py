"""Unit tests for `bot.features.check_in` — detector + episodic-file writer."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.bot.features.check_in import (
    append_to_episodic_section,
    capture_check_in_reply,
    confirmation_text,
    detect_check_in_kind,
    episodic_file_for,
    format_reply_block,
    section_header_for,
)


class TestDetectCheckInKind:
    def test_am_marker_in_reply(self):
        assert detect_check_in_kind("🌅 *AM check-in* — ответь реплаем") == "am"

    def test_am_morning_routine_marker(self):
        assert detect_check_in_kind("☀️ *Утренняя рутина* — отметь") == "am"

    def test_pm_marker_in_reply(self):
        assert detect_check_in_kind("🌙 *PM check-in* — ответь реплаем") == "pm"

    def test_pm_reflection_marker(self):
        assert detect_check_in_kind("Заполни ## PM рефлексия за сегодня") == "pm"

    def test_unrelated_message_returns_none(self):
        assert detect_check_in_kind("Привет, как дела?") is None

    def test_empty_string_returns_none(self):
        assert detect_check_in_kind("") is None

    def test_none_input_returns_none(self):
        assert detect_check_in_kind(None) is None

    def test_am_wins_when_both_markers_present(self):
        # Defensive — shouldn't happen with the YAML protocol, but worth pinning.
        assert detect_check_in_kind("AM check-in вместе с PM check-in") == "am"


class TestSectionHeaderFor:
    def test_am_header(self):
        assert section_header_for("am") == "## AM check-in"

    def test_pm_header(self):
        assert section_header_for("pm") == "## PM рефлексия"


class TestEpisodicFileFor:
    def test_path_uses_iso_date(self, tmp_path):
        ts = datetime(2026, 4, 30, 9, 11, tzinfo=UTC)
        result = episodic_file_for(ts, tmp_path)
        assert result == tmp_path / "2026-04-30.md"


class TestFormatReplyBlock:
    def test_single_line_reply(self):
        ts = datetime(2026, 4, 30, 9, 14, tzinfo=UTC)
        block = format_reply_block("8, 7, бот, панды", ts)
        assert "_09:14 UTC_" in block
        assert "> 8, 7, бот, панды" in block

    def test_multiline_reply_keeps_line_breaks(self):
        ts = datetime(2026, 4, 30, 9, 14, tzinfo=UTC)
        block = format_reply_block("1. 8\n2. 7\n3. бот", ts)
        # Each non-empty line gets a `> ` prefix.
        assert "> 1. 8" in block
        assert "> 2. 7" in block
        assert "> 3. бот" in block

    def test_blank_line_becomes_empty_blockquote(self):
        ts = datetime(2026, 4, 30, 9, 14, tzinfo=UTC)
        block = format_reply_block("foo\n\nbar", ts)
        assert "> foo" in block
        assert ">\n" in block  # blank line preserved as `>`
        assert "> bar" in block


class TestAppendToEpisodicSection:
    def test_creates_file_with_frontmatter_when_missing(self, tmp_path):
        target = tmp_path / "2026-04-30.md"
        append_to_episodic_section(target, "## AM check-in", "first reply\n")

        content = target.read_text(encoding="utf-8")
        assert content.startswith("---\n")
        assert "date: 2026-04-30" in content
        assert "type: daily" in content
        assert "## AM check-in" in content
        assert "first reply" in content

    def test_adds_section_when_file_exists_without_section(self, tmp_path):
        target = tmp_path / "2026-04-30.md"
        target.write_text(
            "---\ndate: 2026-04-30\n---\n\n# 2026-04-30\n\nsome content\n",
            encoding="utf-8",
        )

        append_to_episodic_section(target, "## AM check-in", "the reply\n")

        content = target.read_text(encoding="utf-8")
        assert "some content" in content
        assert "## AM check-in" in content
        assert "the reply" in content
        # Existing content preserved
        assert content.index("some content") < content.index("## AM check-in")

    def test_appends_when_section_already_exists(self, tmp_path):
        target = tmp_path / "2026-04-30.md"
        target.write_text(
            "---\ndate: 2026-04-30\n---\n\n# 2026-04-30\n\n"
            "## AM check-in\n\n_09:00 UTC_\n\n> first answer\n",
            encoding="utf-8",
        )

        append_to_episodic_section(
            target, "## AM check-in", "_09:30 UTC_\n\n> second answer\n"
        )

        content = target.read_text(encoding="utf-8")
        # Both replies present, second after first.
        assert "first answer" in content
        assert "second answer" in content
        assert content.index("first answer") < content.index("second answer")
        # Section header NOT duplicated.
        assert content.count("## AM check-in") == 1

    def test_atomic_write_no_tmp_left_behind(self, tmp_path):
        target = tmp_path / "2026-04-30.md"
        append_to_episodic_section(target, "## AM check-in", "body\n")

        # No `.tmp` file leaks into the dir.
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == [], f"tmp files leaked: {leftovers}"


class TestCaptureCheckInReply:
    def test_writes_under_correct_section_for_am(self, tmp_path):
        ts = datetime(2026, 4, 30, 9, 14, tzinfo=UTC)
        path = capture_check_in_reply(
            kind="am", raw_text="8, 7, бот, панды", episodic_dir=tmp_path, now=ts
        )

        content = path.read_text(encoding="utf-8")
        assert "## AM check-in" in content
        assert "> 8, 7, бот, панды" in content
        assert "_09:14 UTC_" in content

    def test_writes_under_correct_section_for_pm(self, tmp_path):
        ts = datetime(2026, 4, 30, 22, 5, tzinfo=UTC)
        path = capture_check_in_reply(
            kind="pm", raw_text="всё ок", episodic_dir=tmp_path, now=ts
        )

        content = path.read_text(encoding="utf-8")
        assert "## PM рефлексия" in content
        assert "> всё ок" in content

    def test_creates_episodic_dir_if_missing(self, tmp_path):
        # Pass a sub-path that doesn't exist yet.
        nested = tmp_path / "deeply" / "nested" / "episodic"
        capture_check_in_reply(
            kind="am",
            raw_text="hi",
            episodic_dir=nested,
            now=datetime(2026, 4, 30, tzinfo=UTC),
        )
        assert (nested / "2026-04-30.md").exists()

    def test_two_replies_same_day_stack(self, tmp_path):
        ts1 = datetime(2026, 4, 30, 9, 14, tzinfo=UTC)
        ts2 = datetime(2026, 4, 30, 9, 30, tzinfo=UTC)

        capture_check_in_reply(
            kind="am", raw_text="first", episodic_dir=tmp_path, now=ts1
        )
        capture_check_in_reply(
            kind="am", raw_text="second", episodic_dir=tmp_path, now=ts2
        )

        content = (tmp_path / "2026-04-30.md").read_text(encoding="utf-8")
        assert content.count("## AM check-in") == 1
        assert "first" in content
        assert "second" in content
        assert "_09:14 UTC_" in content
        assert "_09:30 UTC_" in content


class TestConfirmationText:
    def test_am_label(self):
        assert "AM" in confirmation_text("am")

    def test_pm_label(self):
        assert "PM" in confirmation_text("pm")
