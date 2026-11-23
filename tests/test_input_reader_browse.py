import io
import os
import queue
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import app.input_reader as ir
from app.input_reader import _browse_directory


class FakeQueue:
    """Queue-like object that can inject Empty timeouts then return keys."""

    def __init__(self, items):
        # items: list where each element is either a key string or None for Empty
        self._items = list(items)

    def get(self, timeout=None):
        if not self._items:
            raise queue.Empty()
        item = self._items.pop(0)
        if item is None:
            raise queue.Empty()
        return item


class BrowseDirectoryTest(unittest.TestCase):
    def setUp(self):
        self._orig_reader = ir._reader_thread
        self._orig_queue = ir._char_queue
        self._orig_shutdown = ir._shutdown_event.is_set()
        ir._shutdown_event.clear()
        fake_thread = mock.Mock()
        fake_thread.is_alive.return_value = True
        ir._reader_thread = fake_thread

    def tearDown(self):
        ir._reader_thread = self._orig_reader
        ir._char_queue = self._orig_queue
        if self._orig_shutdown:
            ir._shutdown_event.set()
        else:
            ir._shutdown_event.clear()

    def _run_browse(self, items, start_path, term_lines=24):
        ir._char_queue = FakeQueue(items)
        buf = io.StringIO()
        term_size = os.terminal_size((80, term_lines))
        with mock.patch("app.input_reader.os.get_terminal_size", return_value=term_size):
            with redirect_stdout(buf):
                result = _browse_directory(start_path, "en")
        return result, buf.getvalue()

    def test_returns_none_when_reader_not_active(self):
        ir._reader_thread = None
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(_browse_directory(td, "en"))

    def test_enter_selects_highlighted_subdir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "alpha").mkdir()
            (root / "beta").mkdir()
            # entries sorted: .., alpha, beta ; move down once to alpha, Enter selects it
            result, out = self._run_browse(["\x1b[HIST_DOWN", "\r"], root)
            self.assertEqual(result, (root / "alpha").resolve())
            self.assertIn("alpha", out)

    def test_enter_on_parent_selects_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "child"
            child.mkdir()
            # cursor starts at .., Enter selects parent of current dir
            result, _ = self._run_browse(["\r"], child)
            self.assertEqual(result, root.resolve())

    def test_right_enters_dir_left_goes_up(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sub = root / "sub"
            sub.mkdir()
            (sub / "inner").mkdir()
            # Right on sub (cursor down once) enters sub, then Left goes up, then q cancel
            result, out = self._run_browse(
                ["\x1b[HIST_DOWN", "\x1b[NAV_RIGHT", "\x1b[NAV_LEFT", "q"],
                root,
            )
            self.assertIsNone(result)
            # after entering sub, title should include sub path at some point
            self.assertIn(str(sub.resolve()), out)
            self.assertIn(str(root.resolve()), out)

    def test_q_cancels_and_clears_screen(self):
        with tempfile.TemporaryDirectory() as td:
            result, out = self._run_browse(["q"], td)
            self.assertIsNone(result)
            # clear-screen escape present
            self.assertIn("\x1b[2J\x1b[H", out)

    def test_filter_typing_backspace_and_esc_clears(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "abc").mkdir()
            (root / "xyz").mkdir()
            # type 'a' to filter, backspace removes, ESC clears filter state; then cancel
            result, out = self._run_browse(["a", "\x7f", "\x1b", "q"], root)
            self.assertIsNone(result)
            self.assertIn("Filtro: a", out)
            # after esc clear and rebuild, xyz should be visible again
            self.assertIn("xyz", out)

    def test_scroll_keeps_cursor_in_view_long_listing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # create many dirs so list exceeds view height
            for i in range(30):
                (root / f"d{i:02d}").mkdir()
            # move down many times then select current
            downs = ["\x1b[HIST_DOWN"] * 25
            result, out = self._run_browse(downs + ["\r"], root, term_lines=10)
            self.assertIsNotNone(result)
            # cursor should have moved well past initial viewport; selected should be a dXX dir
            self.assertTrue(result.name.startswith("d"))

    def test_idle_timeout_does_not_redraw(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sub").mkdir()
            # None simulates queue.Empty/idle timeout; should not cause extra full redraws
            result, out = self._run_browse([None, None, None, "q"], root)
            self.assertIsNone(result)
            # Full clear-screen redraw marker appears on real redraws: initial render + final q clear.
            clears = out.count("\x1b[2J\x1b[H")
            self.assertEqual(clears, 2, f"expected exactly 2 clear-screen redraws, got {clears}")

    def test_cursor_hidden_during_browse_and_restored(self):
        with tempfile.TemporaryDirectory() as td:
            result, out = self._run_browse(["q"], td)
            self.assertIsNone(result)
            self.assertIn("\x1b[?25l", out)
            self.assertIn("\x1b[?25h", out)
            self.assertLess(out.index("\x1b[?25l"), out.rindex("\x1b[?25h"))

    def test_unreadable_directory_falls_back_to_parent_entry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # simulate iterdir failure: entries become just [".."]
            with mock.patch("pathlib.Path.iterdir", side_effect=PermissionError("denied")):
                ir._char_queue = FakeQueue(["q"])
                buf = io.StringIO()
                term_size = os.terminal_size((80, 24))
                with mock.patch("app.input_reader.os.get_terminal_size", return_value=term_size):
                    with redirect_stdout(buf):
                        result = _browse_directory(root, "en")
            self.assertIsNone(result)
            self.assertIn("..", buf.getvalue())

    def test_shutdown_event_during_idle_raises(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ir._char_queue = FakeQueue([None])  # idle tick
            ir._shutdown_event.set()
            buf = io.StringIO()
            term_size = os.terminal_size((80, 24))
            with mock.patch("app.input_reader.os.get_terminal_size", return_value=term_size):
                with redirect_stdout(buf):
                    with self.assertRaises(ir.ShutdownRequested):
                        _browse_directory(root, "en")


if __name__ == "__main__":
    unittest.main()
