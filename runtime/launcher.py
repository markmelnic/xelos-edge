"""Interactive launcher — `xelos` default entry. Keyboard-only line UI."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

from . import __version__
from .capabilities import detect as detect_capabilities
from .config import Credentials, credentials_path


import os as _os


def _default_api() -> str:
    explicit = (_os.environ.get("XELOS_API_BASE") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    if _os.environ.get("XELOS_DEV") in ("1", "true", "yes"):
        return "http://localhost:8000"
    return "https://xelos-api-production.up.railway.app"


_DEFAULT_API = _default_api()

_BOLD = "\033[1m"
_DIM = "\033[2m"
_ACCENT = "\033[38;5;183m"
_OK = "\033[38;5;77m"
_WARN = "\033[38;5;220m"
_INVERT = "\033[7m"
_RESET = "\033[0m"


def _supports_ansi() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def _c(s: str, code: str) -> str:
    return f"{code}{s}{_RESET}" if _supports_ansi() else s


def _clear_screen() -> None:
    """Wipe the viewport + park the cursor at row 1 col 1."""
    if _supports_ansi():
        sys.stdout.write("\x1b[H\x1b[2J")
        sys.stdout.flush()
    else:
        sys.stdout.write("\f")
        sys.stdout.flush()


def _read_key() -> str:
    """Read a normalised key name. Raises KeyboardInterrupt on Ctrl-C."""
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        return line.strip()[:1]

    try:
        import msvcrt  # type: ignore[import-not-found]

        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            ch2 = msvcrt.getch()
            return {
                b"H": "up",
                b"P": "down",
                b"K": "left",
                b"M": "right",
            }.get(ch2, "")
        if ch == b"\r" or ch == b"\n":
            return "enter"
        if ch == b"\x1b":
            return "esc"
        if ch == b"\x03":
            raise KeyboardInterrupt
        return ch.decode("utf-8", errors="ignore")
    except ImportError:
        pass

    import select
    import termios
    import tty

    # Use raw `os.read(fd, ...)`; the text wrapper's buffer hides follow-up
    # bytes from `select()`, breaking arrow-key escape sequence detection.
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        first = os.read(fd, 1)
        if first == b"\x03":
            raise KeyboardInterrupt
        if first in (b"\r", b"\n"):
            return "enter"
        if first == b"\x1b":
            # 150ms is generous over SSH/PTY links and still feels instant for plain Esc.
            ready, _, _ = select.select([fd], [], [], 0.15)
            if not ready:
                return "esc"
            tail = os.read(fd, 8)
            if tail.startswith(b"[A"):
                return "up"
            if tail.startswith(b"[B"):
                return "down"
            if tail.startswith(b"[C"):
                return "right"
            if tail.startswith(b"[D"):
                return "left"
            return "esc"
        try:
            return first.decode("utf-8", errors="ignore")
        except Exception:
            return ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# --- Menu rendering --------------------------------------------------------


@dataclass(slots=True)
class _MenuItem:
    key: str
    action: str
    label: str
    enabled: bool


def _menu_items(creds: Credentials | None) -> list[_MenuItem]:
    items: list[_MenuItem] = [
        _MenuItem("1", "pair", "re-pair" if creds else "pair device", True),
        _MenuItem("2", "serve", "serve  (start daemon + dashboard)", creds is not None),
        _MenuItem("3", "status", "status", True),
        _MenuItem("4", "doctor", "doctor (check tooling)", True),
        _MenuItem("5", "update", "update xelos-edge", True),
    ]
    if creds is not None:
        items.append(_MenuItem("6", "logout", "logout (wipe credentials)", True))
    items.append(_MenuItem("q", "quit", "quit", True))
    return items


def _render_menu(
    creds: Credentials | None,
    items: list[_MenuItem],
    selected: int,
    note: str | None,
) -> None:
    _clear_screen()
    print()
    print(_c(f"xelos-edge {__version__}", _BOLD + _ACCENT))
    if creds is not None:
        print(
            _c(
                f"  paired   device={creds.device_id}  api={creds.api_base}",
                _OK,
            )
        )
    else:
        print(_c("  unpaired — select [1] to enroll this host", _WARN))
    print()

    for i, item in enumerate(items):
        is_sel = i == selected
        bracket = _c(f"[{item.key}]", _ACCENT if item.enabled else _DIM)
        cursor = _c("›", _ACCENT) if is_sel else " "
        body = item.label
        if not item.enabled:
            body = body + "  (paired host required)"
        line = f" {cursor} {bracket} {body}"
        if is_sel and item.enabled and _supports_ansi():
            line = _INVERT + line + _RESET
        elif not item.enabled:
            line = _c(line, _DIM)
        print(line)

    print()
    print(_c("↑↓ move   1-6/q jump   enter select   esc quit", _DIM))
    if note:
        print()
        print(_c(note, _DIM))


def _initial_selection(items: list[_MenuItem]) -> int:
    for i, it in enumerate(items):
        if it.enabled:
            return i
    return 0


def _move(items: list[_MenuItem], idx: int, delta: int) -> int:
    """Step idx by delta, skipping disabled rows. Wraps top↔bottom."""
    n = len(items)
    cur = idx
    for _ in range(n):
        cur = (cur + delta) % n
        if items[cur].enabled:
            return cur
    return idx


# --- Sub-screens (rendered after clearing the viewport) --------------------


def _show_status(creds: Credentials | None) -> None:
    _clear_screen()
    print()
    print(_c("status", _BOLD))
    if creds is None:
        rows: list[tuple[str, str]] = [
            ("state", "not paired"),
            ("credentials", str(credentials_path())),
        ]
    else:
        rows = [
            ("device_id", str(creds.device_id)),
            ("user_id", str(creds.user_id)),
            ("api_base", creds.api_base),
            ("ws_url", creds.websocket_url),
            ("credentials", str(credentials_path())),
        ]
    for k, v in rows:
        print(f"  {_c(k.ljust(14), _DIM)} {v}")
    print()
    _press_any_key()


def _show_doctor() -> None:
    caps = detect_capabilities()
    _clear_screen()
    print()
    print(_c("doctor", _BOLD))
    rows = [
        ("claude_code", caps.get("claude_code_version") or _c("missing", _WARN)),
        ("node", caps.get("node_version") or _c("missing", _WARN)),
        ("python", caps.get("python_version") or "-"),
        ("max_concurrent_runs", str(caps.get("max_concurrent_runs") or "-")),
    ]
    for k, v in rows:
        print(f"  {_c(k.ljust(20), _DIM)} {v}")
    print()
    _press_any_key()


def _confirm(prompt: str) -> bool:
    print()
    print(f"  {prompt} [y/N] ", end="", flush=True)
    try:
        ch = _read_key()
    except KeyboardInterrupt:
        print()
        return False
    print(ch if ch and ch not in {"enter", "esc", "up", "down", "left", "right"} else "")
    return ch.lower() == "y"


def _pair_form() -> dict[str, str] | None:
    _clear_screen()
    print()
    print(_c("pair device", _BOLD))
    try:
        code = input("  pair_code> ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not code:
        print(_c("  cancelled (empty code)", _DIM))
        _press_any_key()
        return None
    try:
        api = (
            input(f"  api_base  [default={_DEFAULT_API}]> ").strip()
            or _DEFAULT_API
        )
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return {"code": code, "api": api}


def _press_any_key() -> None:
    print(_c("press any key…", _DIM), end="", flush=True)
    try:
        _read_key()
    except KeyboardInterrupt:
        pass
    print()


# --- Public entrypoint -----------------------------------------------------


def run_launcher() -> Any:
    """Render the menu and dispatch a single user-chosen action."""
    creds = Credentials.load()
    items = _menu_items(creds)
    selected = _initial_selection(items)
    note: str | None = None

    while True:
        _render_menu(creds, items, selected, note)
        note = None

        try:
            key = _read_key()
        except KeyboardInterrupt:
            print()
            return None

        if key == "up":
            selected = _move(items, selected, -1)
            continue
        if key == "down":
            selected = _move(items, selected, 1)
            continue

        # Shortcut: jump to entry and immediately activate if enabled.
        if key and key not in {"enter", "esc", "left", "right"}:
            for i, it in enumerate(items):
                if it.key == key.lower():
                    if not it.enabled:
                        note = f"option [{it.key}] disabled — pair this host first"
                    else:
                        selected = i
                        chosen = it
                        result = _activate(chosen, creds)
                        if result is _CONTINUE:
                            creds = Credentials.load()
                            items = _menu_items(creds)
                            selected = min(selected, len(items) - 1)
                            continue
                        return result
                    break
            else:
                note = f"unknown key: {key!r}"
            continue

        if key == "esc":
            return None

        if key == "enter":
            chosen = items[selected]
            if not chosen.enabled:
                note = "disabled — pair this host first"
                continue
            result = _activate(chosen, creds)
            if result is _CONTINUE:
                creds = Credentials.load()
                items = _menu_items(creds)
                selected = min(selected, len(items) - 1)
                continue
            return result


# Sentinel for "stay in the launcher loop" — distinct from `None` (quit).
class _Continue:
    __slots__ = ()


_CONTINUE = _Continue()


def _activate(item: _MenuItem, creds: Credentials | None) -> Any:
    """Run an item. Returns a bubble-up value or `_CONTINUE` to stay."""
    if item.action == "quit":
        return None
    if item.action == "pair":
        result = _pair_form()
        if result is None:
            return _CONTINUE
        return {"action": "pair", **result}
    if item.action == "serve":
        return "serve"
    if item.action == "status":
        _show_status(creds)
        return _CONTINUE
    if item.action == "doctor":
        _show_doctor()
        return _CONTINUE
    if item.action == "update":
        if _confirm("run `xelos update`? xelos will exit and pip-upgrade."):
            return "update"
        return _CONTINUE
    if item.action == "logout":
        if _confirm("wipe local credentials? server-side device NOT revoked."):
            return "logout"
        return _CONTINUE
    return _CONTINUE


__all__ = ["run_launcher"]
