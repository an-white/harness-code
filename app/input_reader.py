"""Terminal input reader with ESC×2 cancel/shutdown support (Linux, tty only).

Overview
--------
``start_reader()`` spawns a daemon reader thread when stdin is a tty (POSIX
only; on Windows, non-tty stdin or any failure everything falls back to the
builtin ``input()`` with identical behavior). The thread puts the terminal in
cbreak mode (ICANON/ECHO off) and pushes decoded characters into a queue
consumed by :func:`_read_input`.

Escape handling
---------------
- A ``0x1b`` byte followed by further bytes within ~30 ms is an escape
  *sequence* (arrow keys, F-keys, mouse, ...): recognized sequences emit tokens
  (arrows, Delete, SGR mouse clicks) and the rest are discarded.
- A ``0x1b`` byte followed by silence is a *bare* ESC.
- Two bare ESC presses within 0.5 s (the double-ESC window) request:
  - cancel of the running agent (honoured at the next stream step boundary)
    while an agent is running (``_agent_running`` set) -> ``_cancel_event``; or
  - graceful shutdown while idle -> ``_shutdown_event``.

The double-ESC must be pressed twice QUICKLY: a slow second press starts a new
0.5 s window instead of triggering. Real-terminal behavior must be verified
manually (the automated environment has no tty).

Terminal restoration: the original termios attributes are saved at start and
restored on all exit paths via ``atexit`` and ``restore_terminal()`` (which
cli.py also calls in a ``finally``).
"""

from __future__ import annotations

import atexit
import codecs
import os
import queue
import re
import select
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.i18n import tr

__all__ = [
    "ShutdownRequested",
    "start_reader",
    "restore_terminal",
    "_read_input",
    "_browse_directory",
    "_classify_escape",
    "_cancel_event",
    "_shutdown_event",
    "_agent_running",
    "set_active_agent",
    "get_active_agent",
]

_DOUBLE_ESC_WINDOW = 0.5        # seconds between the two bare ESC presses
_SEQUENCE_READ_TIMEOUT = 0.03   # seconds to wait for ESC follow-up bytes
_MAX_BUFFERED_CHARS = 1000      # drain the queue beyond this to cap memory
_SGR_MOUSE_RE = re.compile(r"^\[<(\d+);(\d+);(\d+)([mM])$")

# Mouse reporting (SGR extended + basic press/release) enabled only during
# interactive tty input when configured/supported, and always restored.
_MOUSE_ENABLE_SEQ = "\x1b[?1000h\x1b[?1006h"
_MOUSE_DISABLE_SEQ = "\x1b[?1000l\x1b[?1006l"


class ShutdownRequested(BaseException):
    """User requested shutdown (double-ESC while idle).

    Inherits from ``BaseException`` (not ``Exception``) so broad
    ``except Exception`` handlers in the pipeline never swallow it; it is a
    control-flow signal akin to ``KeyboardInterrupt``.
    """


# Module-level singletons shared with cli.py.
_cancel_event = threading.Event()     # ESC×2 while an agent is running
_shutdown_event = threading.Event()   # ESC×2 while idle
_agent_running = threading.Event()    # set while _run_graph/run_onboarding run

_reader_thread: threading.Thread | None = None
_char_queue: "queue.Queue[str]" = queue.Queue()
_saved_termios: Any = None
_stop_reader = threading.Event()
_atexit_registered = False

# Command history (shared with CLI prompts)
_command_history: list[str] = []
_history_index: int | None = None

# Active agent/role context for prompt styling hooks (UIX-2/UIX-3).
# Defaults to None/pm at call sites as appropriate; kept module-level so CLI
# flows can set context without threading it through every prompt call yet.
_active_agent_role: str | None = None


def _env_bool_simple(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _ui_colors_enabled() -> bool:
    """Return whether UI colors/badges/highlight are enabled (env/config)."""
    try:
        from app.config import get_settings

        return bool(get_settings().ui_colors)
    except Exception:
        return _env_bool_simple("UI_COLORS", True)


def _ui_mouse_enabled() -> bool:
    """Return whether best-effort mouse cursor positioning is enabled."""
    try:
        from app.config import get_settings

        return bool(get_settings().ui_mouse)
    except Exception:
        return _env_bool_simple("UI_MOUSE", True)


def _classify_escape(had_followup: bool) -> str:
    """Classify a 0x1b byte: ``"sequence"`` if follow-up bytes arrived, else ``"esc"``."""
    return "sequence" if had_followup else "esc"


def _read_escape_followup(fd: int) -> bytes:
    """Bounded wait (~30 ms) for bytes following ESC.

    Returns the follow-up bytes read (empty when none arrived in time, i.e. a
    bare ESC). Bytes are drained until the input goes quiet so the whole
    sequence (e.g. ``ESC [ A`` or SGR mouse ``ESC [ < 0;col;row M``) is
    consumed.
    """
    chunks = bytearray()
    while True:
        ready, _, _ = select.select([fd], [], [], _SEQUENCE_READ_TIMEOUT)
        if not ready:
            break
        try:
            data = os.read(fd, 64)
        except OSError:
            break
        if not data:
            break
        chunks.extend(data)
    return bytes(chunks)


def _drain_if_needed() -> None:
    """Cap queue memory: drain everything when it exceeds the limit."""
    if _char_queue.qsize() <= _MAX_BUFFERED_CHARS:
        return
    try:
        while True:
            _char_queue.get_nowait()
    except queue.Empty:
        pass


def _decode_escape_sequence(followup: bytes) -> str | None:
    """Map an ESC follow-up byte sequence to an internal token, or None.

    Unrecognized sequences return None (discarded) to preserve prior behavior.
    """
    try:
        text = followup.decode("ascii", errors="ignore")
    except Exception:
        return None
    if followup == b"[A":
        return "\x1b[HIST_UP]"
    if followup == b"[B":
        return "\x1b[HIST_DOWN]"
    if followup == b"[C":
        return "\x1b[NAV_RIGHT]"
    if followup == b"[D":
        return "\x1b[NAV_LEFT]"
    if followup == b"[3~":
        return "\x1b[DELETE]"
    m = _SGR_MOUSE_RE.match(text)
    if m:
        btn, col, row, kind = m.group(1), m.group(2), m.group(3), m.group(4)
        # Token format: ESC[MOUSE;<btn>;<col>;<row>;<M|m>]
        return f"\x1b[MOUSE;{btn};{col};{row};{kind}]"
    return None


def _reader_loop(fd: int) -> None:
    """Reader thread body: byte-at-a-time reads, ESC classification, enqueue."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    last_esc = 0.0
    try:
        while not _stop_reader.is_set():
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            try:
                data = os.read(fd, 1)
            except OSError:
                break
            if not data:
                break  # EOF on stdin
            if data[0] == 0x1B:
                # ESC: sequence or bare? Wait briefly for follow-up bytes.
                followup = _read_escape_followup(fd)
                kind = _classify_escape(bool(followup))
                if kind == "sequence":
                    token = _decode_escape_sequence(followup)
                    if token is not None:
                        _char_queue.put(token)
                    # Reset double-ESC state so a later bare ESC is not falsely paired.
                    last_esc = 0.0
                    continue
                now = time.monotonic()
                is_double = (now - last_esc) <= _DOUBLE_ESC_WINDOW
                last_esc = now
                if is_double:
                    if _agent_running.is_set():
                        _cancel_event.set()
                    else:
                        _shutdown_event.set()
                continue
            ch = decoder.decode(data)
            if not ch:
                continue
            for c in ch:
                _char_queue.put(c)
            _drain_if_needed()
    finally:
        # Nothing to clean here; termios restore is handled by
        # restore_terminal() (atexit + cli.py finally).
        return None  # noqa: PLR1711


def start_reader() -> bool:
    """Start the reader thread when stdin is a tty (POSIX only).

    Returns ``True`` when the reader is active; ``False`` when falling back to
    builtin ``input()`` (non-tty, non-POSIX or termios failure).
    """
    global _reader_thread, _saved_termios, _atexit_registered
    if _reader_thread is not None and _reader_thread.is_alive():
        return True
    if os.name != "posix" or not sys.stdin.isatty():
        return False
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        _saved_termios = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    except Exception:
        _saved_termios = None
        return False
    _stop_reader.clear()
    _reader_thread = threading.Thread(target=_reader_loop, args=(fd,), name="input-reader", daemon=True)
    _reader_thread.start()
    if not _atexit_registered:
        atexit.register(restore_terminal)
        _atexit_registered = True
    return True


def restore_terminal() -> None:
    """Restore the saved termios attributes and stop the reader (idempotent)."""
    global _saved_termios, _reader_thread
    _stop_reader.set()
    if _reader_thread is not None and _reader_thread.is_alive():
        try:
            _reader_thread.join(timeout=0.3)
        except Exception:
            pass
    if _saved_termios is not None:
        try:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, _saved_termios)
        except Exception:
            pass
        _saved_termios = None


def add_to_history(cmd: str) -> None:
    global _history_index
    if not cmd:
        return
    if _command_history and _command_history[-1] == cmd:
        _history_index = None
        return
    _command_history.append(cmd)
    _history_index = None


def set_active_agent(role: str | None) -> str | None:
    """Set current/active agent role context for prompts. Returns previous value."""
    global _active_agent_role
    prev = _active_agent_role
    _active_agent_role = role
    return prev


def get_active_agent() -> str | None:
    """Return current/active agent role context for prompts."""
    return _active_agent_role


def history_previous() -> str | None:
    global _history_index
    if not _command_history:
        return None
    if _history_index is None:
        _history_index = len(_command_history) - 1
    else:
        _history_index = max(0, _history_index - 1)
    return _command_history[_history_index]


def history_next() -> str | None:
    global _history_index
    if not _command_history:
        return None
    if _history_index is None:
        return None
    _history_index += 1
    if _history_index >= len(_command_history):
        _history_index = None
        return ""
    return _command_history[_history_index]


def history_reset() -> None:
    global _history_index
    _history_index = None


def _strip_ansi_local(text: str) -> str:
    # Local import-avoidance not needed, but keep cheap path; UI helper exists.
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z<>]", "", text or "")


def _visible_len(text: str) -> int:
    """Best-effort visible length for prompt/cursor positioning (ANSI stripped)."""
    try:
        from app.ui import strip_ansi

        plain = strip_ansi(text or "")
    except Exception:
        plain = _strip_ansi_local(text or "")
    return len(plain)


def _resolve_input_role(agent_role: str | None) -> str:
    role = agent_role if agent_role is not None else _active_agent_role
    if not role or not str(role).strip():
        return "pm"
    return str(role).strip()


def _build_input_prompt(prompt: str, role: str | None, *, colors: bool) -> tuple[str, int]:
    """Return (display_prompt, visible_width) for the input line prefix.

    Includes an agent-identifying badge and, when colors are supported/enabled,
    a soft background highlight for the input field/line area. Always resets
    styles so they do not leak past the prompt rendering.
    """
    resolved = _resolve_input_role(role)
    plain_prefix = prompt or ""
    if not colors:
        try:
            from app.ui import format_agent_badge

            badge = format_agent_badge(resolved, stream=sys.stdout, force_color=False)
        except Exception:
            badge = f"[{resolved.capitalize()}]"
        display = f"{badge} {plain_prefix}" if plain_prefix else badge
        return display, _visible_len(display)

    try:
        from app.ui import ANSI_RESET, colorize, format_agent_badge, get_identity, supports_color

        if not supports_color(sys.stdout, force=None):
            badge = format_agent_badge(resolved, stream=sys.stdout, force_color=False)
            display = f"{badge} {plain_prefix}" if plain_prefix else badge
            return display, _visible_len(display)
        identity = get_identity(resolved)
        badge = format_agent_badge(resolved, stream=sys.stdout, force_color=True)
        # Soft/tenue background highlight for the input field/line (after badge).
        # Use role foreground hint for identity while keeping background subtle.
        field_bg = "bright_black"
        field_text = plain_prefix
        field = colorize(field_text, fg=identity.fg, bg=field_bg, bold=False, stream=sys.stdout, force=True)
        # Keep a highlighted space before prompt text so the input field reads as
        # a continuous highlighted region; badge is a distinct colored rectangle.
        spacer = colorize(" ", fg=identity.fg, bg=field_bg, bold=False, stream=sys.stdout, force=True)
        display = f"{badge}{spacer}{field}{ANSI_RESET}"
        return display, _visible_len(display)
    except Exception:
        badge = f"[{resolved.capitalize()}]"
        display = f"{badge} {plain_prefix}" if plain_prefix else badge
        return display, _visible_len(display)


def _render_input_line(prompt_display: str, prompt_width: int, buf: str, cursor: int) -> None:
    """Redraw the input line and place terminal cursor at buffer cursor position."""
    out = sys.stdout
    out.write("\r")
    out.write(prompt_display)
    out.write(buf)
    out.write("\x1b[K")
    # Move cursor from end-of-line back to desired position within buffer.
    tail = len(buf) - max(0, min(cursor, len(buf)))
    if tail > 0:
        out.write(f"\x1b[{tail}D")
    out.flush()


def _parse_mouse_token(token: str) -> tuple[int, int, str, str] | None:
    """Parse SGR mouse token -> (btn, col, row, kind) or None."""
    if not token.startswith("\x1b[MOUSE;"):
        return None
    parts = token.split(";")
    # ['\x1b[MOUSE', btn, col, row, kind]'] but kind may include trailing
    if len(parts) != 5:
        return None
    try:
        btn = int(parts[1])
        col = int(parts[2])
        row = int(parts[3])
        kind = parts[4].rstrip("]")
    except Exception:
        return None
    return btn, col, row, kind


def _read_input(prompt: str, *, use_history: bool = True, agent_role: str | None = None) -> str:
    """Read one line of input.

    ``agent_role`` (or the module active-agent context) identifies the current
    agent/role for prompt styling hooks. Displays an agent badge and a soft
    input-field highlight when supported (ANSI-capable TTY, colors enabled).

    With the reader thread active (tty): consumes queued characters/sequence
    tokens with inline editing (left/right cursor, insert/delete at cursor),
    history navigation, Enter/Ctrl+C/Ctrl+D handling, and best-effort mouse
    click cursor positioning in supported terminals.

    Without the reader (non-tty/POSIX failure): delegates to builtin
    ``input(prompt_text)`` with styled prompt text when safe.
    """
    resolved_role = _resolve_input_role(agent_role)
    colors = _ui_colors_enabled()

    if _reader_thread is None or not _reader_thread.is_alive():
        prompt_display, _ = _build_input_prompt(prompt, resolved_role, colors=colors)
        return input(prompt_display)

    prompt_display, prompt_width = _build_input_prompt(prompt, resolved_role, colors=colors)

    # Best-effort mouse reporting only during interactive input on a TTY.
    mouse_enabled = False
    if _ui_mouse_enabled():
        try:
            if sys.stdout.isatty() and sys.stdin.isatty():
                sys.stdout.write(_MOUSE_ENABLE_SEQ)
                sys.stdout.flush()
                mouse_enabled = True
        except Exception:
            mouse_enabled = False

    buf = ""
    cursor = 0
    history_reset()
    _render_input_line(prompt_display, prompt_width, buf, cursor)
    try:
        while True:
            if _shutdown_event.is_set():
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise ShutdownRequested()
            try:
                ch = _char_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if ch in ("\x1b[HIST_UP]", "\x1b[HIST_UP"):
                if use_history:
                    prev = history_previous()
                    if prev is not None:
                        buf = prev
                        cursor = len(buf)
                        _render_input_line(prompt_display, prompt_width, buf, cursor)
                    else:
                        sys.stdout.write("\x07")
                        sys.stdout.flush()
                continue
            if ch in ("\x1b[HIST_DOWN]", "\x1b[HIST_DOWN"):
                if use_history:
                    nxt = history_next()
                    if nxt is None:
                        sys.stdout.write("\x07")
                        sys.stdout.flush()
                        buf = ""
                        cursor = 0
                    else:
                        buf = nxt
                        cursor = len(buf)
                    _render_input_line(prompt_display, prompt_width, buf, cursor)
                continue

            # Inline cursor movement (arrow keys in text input context).
            if ch in ("\x1b[NAV_LEFT]", "\x1b[NAV_LEFT"):
                if cursor > 0:
                    cursor -= 1
                    _render_input_line(prompt_display, prompt_width, buf, cursor)
                continue
            if ch in ("\x1b[NAV_RIGHT]", "\x1b[NAV_RIGHT"):
                if cursor < len(buf):
                    cursor += 1
                    _render_input_line(prompt_display, prompt_width, buf, cursor)
                continue

            # Delete key (forward delete at cursor), if sequence recognized.
            if ch in ("\x1b[DELETE]", "\x1b[DELETE"):
                if cursor < len(buf):
                    history_reset()
                    buf = buf[:cursor] + buf[cursor + 1:]
                    _render_input_line(prompt_display, prompt_width, buf, cursor)
                continue

            # Mouse click positioning (best-effort, SGR).
            if ch.startswith("\x1b[MOUSE;"):
                mouse = _parse_mouse_token(ch)
                if mouse is not None:
                    btn, col, _row, kind = mouse
                    # Left button press: position cursor. Ignore release/motion.
                    if kind == "M" and btn == 0:
                        # Terminal columns are 1-based; map click to buffer index.
                        target = col - 1 - prompt_width
                        if target < 0:
                            target = 0
                        if target > len(buf):
                            target = len(buf)
                        if target != cursor:
                            cursor = target
                            _render_input_line(prompt_display, prompt_width, buf, cursor)
                continue

            if ch == "\x03":  # Ctrl+C
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise KeyboardInterrupt()
            if ch == "\x04":  # Ctrl+D: treat as EOF to keep existing fallbacks
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise EOFError()
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                if use_history:
                    add_to_history(buf)
                history_reset()
                return buf
            if ch in ("\x7f", "\b"):
                if cursor > 0:
                    history_reset()
                    buf = buf[: cursor - 1] + buf[cursor:]
                    cursor -= 1
                    _render_input_line(prompt_display, prompt_width, buf, cursor)
                continue
            if ch.isprintable():
                # typing while browsing history: start fresh edit from current buf
                history_reset()
                buf = buf[:cursor] + ch + buf[cursor:]
                cursor += len(ch)
                _render_input_line(prompt_display, prompt_width, buf, cursor)
            # other control/unknown tokens are ignored
    finally:
        if mouse_enabled:
            try:
                sys.stdout.write(_MOUSE_DISABLE_SEQ)
                sys.stdout.flush()
            except Exception:
                pass


def _browse_directory(start_path: Path | str, lang: str = "en") -> Path | None:
    """Browse directories with arrow keys; return selected Path or None if cancelled."""
    if _reader_thread is None or not _reader_thread.is_alive():
        return None
    try:
        cur = Path(start_path).expanduser().resolve()
    except Exception:
        return None
    if not cur.is_dir():
        try:
            cur = Path.cwd()
        except Exception:
            return None
    filter_buf = ""
    # Hide cursor during TUI rendering to avoid flicker/cursor instability; restore on exit.
    sys.stdout.write("\x1b[?25l")
    sys.stdout.flush()
    try:
        while True:
            # Build entries: .. then sorted subdirs
            all_entries: list[str] = [".."]
            try:
                for p in sorted(cur.iterdir(), key=lambda x: x.name.lower()):
                    if p.is_dir():
                        all_entries.append(p.name)
            except Exception:
                all_entries = [".."]
            if filter_buf:
                fb = filter_buf.lower()
                entries = [e for e in all_entries if e == ".." or fb in e.lower()]
            else:
                entries = list(all_entries)
            if not entries:
                entries = [".."]
            cursor = 0
            scroll = 0
            needs_redraw = True
            while True:
                # Clamp cursor/scroll and keep selection in view
                cursor = max(0, min(cursor, len(entries) - 1))
                try:
                    term_h = os.get_terminal_size().lines
                except Exception:
                    term_h = 24
                reserved = 4  # title, help, blank line, margin
                if filter_buf:
                    reserved += 1
                view_h = max(1, term_h - reserved)
                prev_scroll = scroll
                scroll = max(0, min(scroll, max(0, len(entries) - view_h)))
                if cursor < scroll:
                    scroll = cursor
                elif cursor >= scroll + view_h:
                    scroll = cursor - view_h + 1
                scroll = max(0, min(scroll, max(0, len(entries) - view_h)))
                if scroll != prev_scroll:
                    needs_redraw = True

                # Redraw visible window only when state changed (not on idle timeout)
                if needs_redraw:
                    sys.stdout.write("\x1b[2J\x1b[H")
                    sys.stdout.write(tr("dir_browse_title", lang).format(path=cur) + "\n")
                    sys.stdout.write(tr("dir_browse_help", lang) + "\n")
                    if filter_buf:
                        sys.stdout.write(f"Filtro: {filter_buf}\n")
                    sys.stdout.write("\n")
                    for i in range(scroll, min(len(entries), scroll + view_h)):
                        name = entries[i]
                        prefix = "> " if i == cursor else "  "
                        sys.stdout.write(f"{prefix}{name}\n")
                    sys.stdout.flush()
                    needs_redraw = False

                # Read key
                try:
                    ch = _char_queue.get(timeout=0.1)
                except queue.Empty:
                    # Idle: do not redraw; keep waiting for input/shutdown.
                    if _shutdown_event.is_set():
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        raise ShutdownRequested()
                    continue

                if ch in ("\x1b[HIST_UP]", "\x1b[HIST_UP"):
                    cursor = max(0, cursor - 1)
                    needs_redraw = True
                    continue
                if ch in ("\x1b[HIST_DOWN]", "\x1b[HIST_DOWN"):
                    cursor = min(len(entries) - 1, cursor + 1)
                    needs_redraw = True
                    continue
                if ch in ("\x1b[NAV_RIGHT]", "\x1b[NAV_RIGHT"):
                    sel = entries[cursor] if entries else ".."
                    if sel == "..":
                        cur = cur.parent
                    else:
                        cur = (cur / sel).resolve()
                    filter_buf = ""
                    break  # rebuild
                if ch in ("\x1b[NAV_LEFT]", "\x1b[NAV_LEFT"):
                    cur = cur.parent
                    filter_buf = ""
                    break  # rebuild
                if ch in ("\r", "\n"):
                    sel = entries[cursor] if entries else ".."
                    if sel == "..":
                        return cur.parent
                    return (cur / sel).resolve()
                if ch == "q":
                    sys.stdout.write("\x1b[2J\x1b[H")
                    sys.stdout.flush()
                    return None
                if ch == "\x1b":  # ESC clears filter
                    filter_buf = ""
                    break  # redraw with cleared filter
                if ch in ("\x7f", "\b"):
                    filter_buf = filter_buf[:-1]
                    break  # redraw
                if len(ch) == 1 and ch.isprintable() and ch not in ("\t",):
                    filter_buf += ch
                    break  # redraw
                if ch == "\x03":
                    raise KeyboardInterrupt()
                if ch == "\x04":
                    raise EOFError()
                # ignore others (no redraw/state change)
    finally:
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()
