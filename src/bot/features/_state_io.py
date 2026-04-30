"""Shared state I/O for check_in_state.json with atomic writes + file lock.

Replaces 9+ duplicated _load_state/_save_state across features/.
Atomic write via tmp+rename. fcntl.flock to prevent concurrent corruption.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False


def state_path(repo: Path) -> Path:
    return repo / "state" / "check_in_state.json"


def load_state(repo: Path) -> dict:
    f = state_path(repo)
    if not f.exists():
        return {}
    try:
        if HAS_FCNTL:
            with f.open("r", encoding="utf-8") as fp:
                fcntl.flock(fp.fileno(), fcntl.LOCK_SH)
                try:
                    return json.load(fp)
                finally:
                    fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        else:
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(repo: Path, state: dict) -> None:
    """Atomic save: write to .tmp, then os.rename (POSIX atomic)."""
    f = state_path(repo)
    f.parent.mkdir(parents=True, exist_ok=True)
    # Write to tempfile in same dir (so rename is atomic on same filesystem)
    fd, tmp_path = tempfile.mkstemp(prefix=".check_in_state.", suffix=".tmp", dir=str(f.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            if HAS_FCNTL:
                fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
            json.dump(state, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
            if HAS_FCNTL:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        os.replace(tmp_path, f)
    except Exception:
        # Cleanup tempfile on error
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def update_state(repo: Path, **updates: Any) -> dict:
    """Read-modify-write under exclusive lock — returns new state."""
    f = state_path(repo)
    f.parent.mkdir(parents=True, exist_ok=True)

    if not HAS_FCNTL:
        # Fallback non-atomic on Windows
        state = load_state(repo)
        state.update(updates)
        save_state(repo, state)
        return state

    # Open with O_RDWR|O_CREAT, lock, modify, write, unlock
    if not f.exists():
        f.write_text("{}", encoding="utf-8")

    with f.open("r+", encoding="utf-8") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        try:
            try:
                state = json.load(fp)
            except json.JSONDecodeError:
                state = {}
            state.update(updates)
            fp.seek(0)
            fp.truncate()
            json.dump(state, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
    return state
