import io
import os
import queue
import unittest
from contextlib import redirect_stdout
from unittest import mock

import app.input_reader as ir


class FakeQueue:
    """Queue-like object that returns queued items and then Empty."""

    def __init__(self, items):
        self._items = list(items)

    def get(self, timeout=None):
        if not self._items:
            raise queue.Empty()
        item = self._items.pop(0)
        if item is None:
            raise queue.Empty()
        return item


class InlineInputReaderTest(unittest.TestCase):
    def setUp(self):
        self._orig_reader = ir._reader_thread
        self._orig_queue = ir._char_queue
        self._orig_shutdown = ir._shutdown_event.is_set()
        self._orig_history = list(ir._command_history)
        self._orig_hist_idx = ir._history_index
        self._orig_active = ir._active_agent_role
        ir._shutdown_event.clear()
        ir._command_history = []
        ir._history_index = None
        ir._active_agent_role = None
        fake_thread = mock.Mock()
        fake_thread.is_alive.return_value = True
        ir._reader_thread = fake_thread

    def tearDown(self):
        ir._reader_thread = self._orig_reader
        ir._char_queue = self._orig_queue
        ir._command_history = self._orig_history
        ir._history_index = self._orig_hist_idx
        ir._active_agent_role = self._orig_active
        if self._orig_shutdown:
            ir._shutdown_event.set()
        else:
            ir._shutdown_event.clear()

    def _read(self, items, prompt="> ", use_history=True, agent_role=None):
        ir._char_queue = FakeQueue(items)
        buf = io.StringIO()
        env = {"UI_COLORS": "0", "UI_MOUSE": "0", "NO_COLOR": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            # Avoid config cache/env interactions affecting UI flags in tests.
            with mock.patch("app.input_reader._ui_colors_enabled", return_value=False), \
                 mock.patch("app.input_reader._ui_mouse_enabled", return_value=False):
                with redirect_stdout(buf):
                    result = ir._read_input(prompt, use_history=use_history, agent_role=agent_role)
        return result, buf.getvalue()

    def test_badge_prefix_and_basic_typing(self):
        result, out = self._read(list("abc") + ["\r"], prompt="Ask: ", agent_role="builder")
        self.assertEqual(result, "abc")
        self.assertIn("[Builder]", out)
        self.assertIn("Ask:", out)

    def test_insert_and_backspace_at_cursor_middle(self):
        # type ab, move left, insert X, move left, backspace (deletes a), enter -> "Xb"
        items = ["a", "b", "\x1b[NAV_LEFT]", "X", "\x1b[NAV_LEFT]", "\x7f", "\r"]
        result, _ = self._read(items)
        self.assertEqual(result, "Xb")

    def test_left_right_cursor_movement_and_append(self):
        # type ab, left,left,right, type Z at middle -> "aZb"
        items = ["a", "b", "\x1b[NAV_LEFT]", "\x1b[NAV_LEFT]", "\x1b[NAV_RIGHT]", "Z", "\r"]
        result, _ = self._read(items)
        self.assertEqual(result, "aZb")

    def test_delete_key_removes_char_at_cursor(self):
        # type abc, left,left, delete removes b -> "ac"
        items = list("abc") + ["\x1b[NAV_LEFT]", "\x1b[NAV_LEFT]", "\x1b[DELETE]", "\r"]
        result, _ = self._read(items)
        self.assertEqual(result, "ac")

    def test_history_up_down_preserves_navigation(self):
        ir._command_history = ["first", "second"]
        ir._history_index = None
        # up -> second, up -> first, down -> second, enter submits
        items = ["\x1b[HIST_UP]", "\x1b[HIST_UP]", "\x1b[HIST_DOWN]", "\r"]
        result, _ = self._read(items)
        self.assertEqual(result, "second")

    def test_history_browse_then_edit_resets_index(self):
        ir._command_history = ["old"]
        ir._history_index = None
        # up loads old, move left, type X inserts -> edits buffer; enter submits edited
        items = ["\x1b[HIST_UP]", "\x1b[NAV_LEFT]", "X", "\r"]
        result, _ = self._read(items)
        self.assertEqual(result, "olXd")

    def test_mouse_click_positions_cursor_best_effort(self):
        # Plain mode prompt display: "[PM] " + "> " => "[PM] > " (visible width 7).
        # Buffer "abcd", click at column placing cursor before 'c' (index 2) => col 10 (1-based).
        items = list("abcd") + ["\x1b[MOUSE;0;10;1;M]", "X", "\r"]
        result, _ = self._read(items, prompt="> ", agent_role="pm")
        self.assertEqual(result, "abXcd")

    def test_mouse_release_ignored(self):
        items = list("ab") + ["\x1b[MOUSE;0;7;1;m]", "c", "\r"]
        result, _ = self._read(items, prompt="> ", agent_role="pm")
        self.assertEqual(result, "abc")

    def test_ctrl_c_raises_keyboard_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            self._read(["a", "\x03"])

    def test_ctrl_d_raises_eof(self):
        with self.assertRaises(EOFError):
            self._read(["\x04"])

    def test_active_agent_context_used_when_role_not_passed(self):
        ir._active_agent_role = "qa"
        result, out = self._read(["\r"], prompt="> ")
        self.assertEqual(result, "")
        self.assertIn("[QA]", out)

    def test_render_uses_clear_to_eol(self):
        result, out = self._read(list("ab") + ["\x7f", "\r"])
        self.assertEqual(result, "a")
        self.assertIn("\x1b[K", out)


if __name__ == "__main__":
    unittest.main()
