"""AM/PM check-in reply capture.

Detects when a user message is a reply to a daily AM/PM check-in ping
(identified by markers in the original message text), and appends the raw
reply text to the episodic file under the configured section.

This is a v1 minimum-viable loop: no parsing of state/sleep/routine_checks
yet — just capture the raw text so it ends up in `tracks/state/episodic/
<today>.md` instead of being routed as journal/idea by inbox-router.

Parsing into structured fields is deferred until we see real reply patterns
across a week of usage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Optional

import structlog

logger = structlog.get_logger()


CheckInKind = Literal["am", "pm"]

# Markers that identify a bot-sent check-in message. Match on the original
# message Гена is replying to (msg.reply_to_message.text). Kept loose enough
# to survive small wording tweaks in the YAML protocol.
_AM_MARKERS: tuple[str, ...] = ("AM check-in", "Утренняя рутина", "🌅")
_PM_MARKERS: tuple[str, ...] = ("PM check-in", "PM рефлексия", "🌙")


def detect_check_in_kind(reply_to_text: Optional[str]) -> Optional[CheckInKind]:
    """Return 'am'/'pm' if `reply_to_text` is a check-in ping, else None.

    AM is checked first; if both sets of markers are present (shouldn't
    happen in practice), AM wins.
    """
    if not reply_to_text:
        return None
    text = reply_to_text
    if any(marker in text for marker in _AM_MARKERS):
        return "am"
    if any(marker in text for marker in _PM_MARKERS):
        return "pm"
    return None


def episodic_file_for(date: datetime, episodic_dir: Path) -> Path:
    """Return the path to the daily episodic file for `date`.

    The file may not exist yet — callers create it via `append_to_episodic_section`.
    """
    return episodic_dir / f"{date.strftime('%Y-%m-%d')}.md"


def _initial_frontmatter(date: datetime) -> str:
    """Minimal frontmatter for a freshly-created episodic file."""
    return (
        "---\n"
        f"date: {date.strftime('%Y-%m-%d')}\n"
        "type: daily\n"
        "---\n"
        "\n"
        f"# {date.strftime('%Y-%m-%d')}\n"
        "\n"
    )


def append_to_episodic_section(
    file_path: Path,
    section_header: str,
    body: str,
) -> None:
    """Append `body` under `section_header` in `file_path`, creating both as needed.

    `section_header` is the markdown heading (e.g. "## AM check-in"). If the
    section doesn't exist, it is added at end of file. If the file doesn't
    exist, a minimal scaffold is created first.

    The write is atomic: write to tmp, then rename.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)

    if file_path.exists():
        existing = file_path.read_text(encoding="utf-8")
    else:
        # Scaffold: derive date from filename when possible.
        try:
            d = datetime.strptime(file_path.stem, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            d = datetime.now(UTC)
        existing = _initial_frontmatter(d)

    if section_header in existing:
        # Append body at the end of the section. We append after the very
        # last line of the file rather than inside the section, to avoid
        # touching anything the user (or a previous tool) wrote there. If the
        # section is the last one, this is identical to "inside section".
        # This is intentional v1 behaviour — keeps the writer dumb.
        if not existing.endswith("\n"):
            existing += "\n"
        new_content = existing + body.rstrip() + "\n"
    else:
        # Add section + body at end.
        if not existing.endswith("\n"):
            existing += "\n"
        if not existing.endswith("\n\n"):
            existing += "\n"
        new_content = existing + f"{section_header}\n\n{body.rstrip()}\n"

    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp_path.write_text(new_content, encoding="utf-8")
    tmp_path.replace(file_path)


def format_reply_block(raw_text: str, ts: datetime) -> str:
    """Format a captured reply as a timestamped blockquote.

    Multiple replies on the same day stack as separate blocks under the
    section. Blockquote (`> `) keeps original line breaks readable and is
    easy to spot visually.
    """
    ts_str = ts.strftime("%H:%M UTC")
    quoted = "\n".join(f"> {line}" if line else ">" for line in raw_text.splitlines())
    return f"_{ts_str}_\n\n{quoted}\n"


def section_header_for(kind: CheckInKind) -> str:
    """Return the markdown header used for AM vs PM responses.

    Matches `storage_section` values in `state/protocols/check_ins.yaml`
    (`## AM check-in` and `## PM рефлексия`).
    """
    return "## AM check-in" if kind == "am" else "## PM рефлексия"


def capture_check_in_reply(
    kind: CheckInKind,
    raw_text: str,
    episodic_dir: Path,
    now: Optional[datetime] = None,
) -> Path:
    """Append `raw_text` to today's episodic file under the AM/PM section.

    Returns the path of the file written. `now` is injectable for tests.
    """
    ts = now or datetime.now(UTC)
    file_path = episodic_file_for(ts, episodic_dir)
    section = section_header_for(kind)
    body = format_reply_block(raw_text, ts)
    append_to_episodic_section(file_path, section, body)
    logger.info(
        "check_in: captured reply",
        kind=kind,
        file=str(file_path),
        bytes=len(raw_text),
    )
    return file_path


def confirmation_text(kind: CheckInKind) -> str:
    """Short confirmation reply sent back in Telegram after capture."""
    label = "AM" if kind == "am" else "PM"
    return f"✅ {label} check-in записан в episodic."
