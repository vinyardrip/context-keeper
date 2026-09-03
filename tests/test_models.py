"""Tests for the AST node mutation methods on ``TaskList``."""

from __future__ import annotations

import unittest

from cklib.models import TaskList, TaskStatus


class TestTaskListMutations(unittest.TestCase):
    def test_add_assigns_unique_ids(self):
        tl = TaskList()
        a = tl.add("first")
        b = tl.add("second")
        c = tl.add("third")
        self.assertEqual([a.id, b.id, c.id], [1, 2, 3])

    def test_focus_demotes_others(self):
        tl = TaskList()
        tl.add("a")
        tl.add("b")
        tl.add("c")
        tl.focus(1)
        self.assertEqual(tl.tasks[0].status, TaskStatus.FOCUSED)
        tl.focus(3)
        self.assertEqual(tl.tasks[0].status, TaskStatus.OPEN)
        self.assertEqual(tl.tasks[2].status, TaskStatus.FOCUSED)

    def test_focus_unknown_raises(self):
        tl = TaskList()
        tl.add("a")
        with self.assertRaises(KeyError):
            tl.focus(99)

    def test_toggle_done_unknown_raises(self):
        tl = TaskList()
        tl.add("a")
        with self.assertRaises(KeyError):
            tl.toggle_done([99])

    def test_toggle_done_skips_already_done(self):
        tl = TaskList()
        tl.add("a")
        tl.add("b")
        result = tl.toggle_done([1])
        self.assertEqual(result, [1])
        # Second call: already done, no transition
        result2 = tl.toggle_done([1])
        self.assertEqual(result2, [])

    def test_edit_note_appends(self):
        tl = TaskList()
        t = tl.add("a")
        tl.edit_note(t.id, "first note")
        tl.edit_note(t.id, "second note")
        self.assertEqual(t.notes, ["first note", "second note"])

    def test_remove_returns_node(self):
        tl = TaskList()
        t1 = tl.add("a")
        t2 = tl.add("b")
        removed = tl.remove(t1.id)
        self.assertEqual(removed.id, 1)
        self.assertEqual(len(tl.tasks), 1)
        self.assertEqual(tl.tasks[0].id, 2)


if __name__ == "__main__":
    unittest.main()