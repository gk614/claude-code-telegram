"""Markdown escape helpers for Telegram parse_mode=MARKDOWN.

Telegram Markdown is fragile — single * or _ in user text → 400 Bad Request.
"""

def escape_md(text: str) -> str:
    """Escape special chars for Telegram Markdown v1."""
    if not text:
        return ""
    out = str(text)
    for ch in ("\\", "*", "_", "[", "]", "`"):
        out = out.replace(ch, f"\\{ch}")
    return out
