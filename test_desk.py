import unittest
import tempfile
import os
import json
import threading
import io
import subprocess
import curses
from unittest.mock import patch

from desk import (agent_label, attention, cleanup_action, cleanup_items, create_worktree,
                  click_kind, desk, draw_cards, keep_visible, load_closed_worktrees, load_pins,
                  load_snapshot, mouse_target, read_agent, rows, save_pins, watch_events)


class Screen:
    def __init__(self, width=140, height=50):
        self.lines = []
        self.width = width
        self.height = height

    def getmaxyx(self):
        return (self.height, self.width)

    def erase(self):
        pass

    def addstr(self, y, x, value, attr):
        self.lines.append((y, value))

    def refresh(self):
        pass


class DeskRowsTest(unittest.TestCase):
    @patch("desk.call")
    def test_snapshot_unwraps_cli_response(self, command):
        command.return_value = {"id": "cli:api:snapshot", "result": {"snapshot": {"workspaces": [], "agents": []}}}
        self.assertEqual(load_snapshot()["agents"], [])
        command.assert_called_once_with("api", "snapshot")

    def test_attention_first_and_filter_includes_its_workspace(self):
        snapshot = {
            "workspaces": [
                {"workspace_id": "w1", "label": "web"},
                {"workspace_id": "w2", "label": "api", "worktree": {"repo_name": "api", "checkout_path": "/tmp/api/fix-auth"}},
            ],
            "agents": [
                {"workspace_id": "w1", "pane_id": "w1:p1", "agent_status": "working", "name": "build-ui"},
                {"workspace_id": "w2", "pane_id": "w2:p1", "agent_status": "blocked", "name": "fix-auth"},
            ],
        }
        self.assertEqual([item[0]["workspace_id"] for item in rows(snapshot)], ["w2", "w1"])
        self.assertEqual([item[0]["workspace_id"] for item in rows(snapshot, "fix-auth")], ["w2"])
        self.assertEqual(rows(snapshot, "missing"), [])

    def test_agent_uses_pane_label_before_id(self):
        agent = {"pane_id": "w1:p1", "tab_id": "w1:t1"}
        snapshot = {"panes": [{"pane_id": "w1:p1", "label": "Review refunds"}], "tabs": []}
        self.assertEqual(agent_label(agent, snapshot), "Review refunds")

    @patch("desk.curses.color_pair", return_value=0)
    @patch("desk.status_style", return_value=0)
    def test_list_shows_label_instead_of_pane_id(self, _, _color):
        snapshot = {
            "workspaces": [{"workspace_id": "w1", "label": "billing", "tab_count": 1, "pane_count": 1}],
            "agents": [{"workspace_id": "w1", "pane_id": "w1:p1", "agent_status": "done"}],
            "panes": [{"pane_id": "w1:p1", "label": "Review refunds"}],
        }
        screen = Screen()
        draw_cards(screen, snapshot, rows(snapshot), 0, "", "")
        list_text = " ".join(value for y, value in screen.lines if y in (17, 18, 19))
        self.assertIn("Review refunds", list_text)
        self.assertNotIn("w1:p1", list_text)
        self.assertFalse(any("billing" in value or "DONE" in value for y, value in screen.lines if y == 16))
        self.assertFalse(any("╭" in value or "╰" in value for _, value in screen.lines))

    @patch("desk.curses.color_pair", return_value=0)
    @patch("desk.status_style", return_value=0)
    def test_narrow_card_layout(self, _, _color):
        snapshot = {"workspaces": [{"workspace_id": "w1", "label": "billing"}], "agents": []}
        screen = Screen(width=60, height=30)
        draw_cards(screen, snapshot, rows(snapshot), 0, "", "")
        self.assertIn("billing", " ".join(value for _, value in screen.lines))

    @patch("desk.curses.color_pair", return_value=0)
    @patch("desk.status_style", return_value=0)
    def test_footer_shows_shortcuts_for_current_mode(self, _, _color):
        snapshot = {"workspaces": [{"workspace_id": "w1", "label": "billing"}], "agents": []}
        screen = Screen(height=74)
        draw_cards(screen, snapshot, rows(snapshot), 0, "", "")
        self.assertIn("F Filter", " ".join(value for y, value in screen.lines if y == 73))
        self.assertNotIn("D Close", " ".join(value for y, value in screen.lines if y == 73))
        screen.lines.clear()
        draw_cards(screen, snapshot, rows(snapshot), 0, "", "", cleanup=True)
        self.assertIn("D Close", " ".join(value for y, value in screen.lines if y == 73))

    def test_mouse_targets_cards_and_detail_actions(self):
        self.assertEqual(mouse_target(74, 140, 0, 17, 1, 9, 17), ("card", 0))
        self.assertEqual(mouse_target(74, 140, 0, 17, 1, 73, 17), ("card", 1))
        self.assertEqual(mouse_target(74, 140, 0, 17, 1, 9, 64), ("agent", 0))
        self.assertEqual(mouse_target(74, 140, 0, 17, 1, 115, 59), ("open", None))
        self.assertEqual(mouse_target(74, 140, 16, 17, 1, 9, 17), ("card", 4))
        self.assertEqual(mouse_target(74, 140, 0, 17, 1, 9, 10, 1), ("attention", 0))
        self.assertEqual(mouse_target(74, 140, 0, 17, 1, 35, 14), ("filter", 1))
        self.assertEqual(mouse_target(74, 140, 0, 17, 1, 115, 59, cleanup=True), ("remove", None))
        self.assertEqual(mouse_target(74, 140, 0, 17, 1, 115, 67), ("resume", None))

    def test_selecting_visible_card_keeps_mouse_target_stable(self):
        view_row = 2
        self.assertEqual(mouse_target(74, 140, 16, 17, 1, 9, 17, view_row=view_row), ("card", 4))
        self.assertEqual(keep_visible(4, 2, 7, view_row), view_row)
        self.assertEqual(mouse_target(74, 140, 4, 17, 1, 9, 17, view_row=view_row), ("card", 4))

    def test_press_and_click_reports_count_once_but_second_press_opens(self):
        press = (None, None, 0.0)
        card_click = (None, 0.0)
        kind, press, card_click = click_kind(curses.BUTTON1_PRESSED, "card", 1, 1.0, press, card_click)
        self.assertEqual(kind, "single")
        kind, press, card_click = click_kind(curses.BUTTON1_CLICKED, "card", 1, 1.1, press, card_click)
        self.assertIsNone(kind)
        kind, _, _ = click_kind(curses.BUTTON1_PRESSED, "card", 1, 1.3, press, card_click)
        self.assertEqual(kind, "double")

    def test_click_only_reports_can_double_open(self):
        press = (None, None, 0.0)
        card_click = (None, 0.0)
        kind, press, card_click = click_kind(curses.BUTTON1_CLICKED, "card", 1, 1.0, press, card_click)
        self.assertEqual(kind, "single")
        kind, _, _ = click_kind(curses.BUTTON1_CLICKED, "card", 1, 1.3, press, card_click)
        self.assertEqual(kind, "double")

    def test_native_double_click_report_opens(self):
        kind, _, _ = click_kind(curses.BUTTON1_DOUBLE_CLICKED | curses.BUTTON1_CLICKED, "card", 1, 1.1,
                                ("card", 1, 1.0), (None, 0.0))
        self.assertEqual(kind, "double")

    def test_double_click_opens_even_while_preview_is_slow(self):
        snapshot = {"workspaces": [{"workspace_id": "w1", "label": "alpha"},
                                   {"workspace_id": "w2", "label": "beta"}],
                    "agents": [{"workspace_id": "w1", "pane_id": "w1:p1", "agent_status": "idle"},
                               {"workspace_id": "w2", "pane_id": "w2:p1", "agent_status": "idle"}]}
        preview_block = threading.Event()

        class InputScreen(Screen):
            keys = iter((curses.KEY_MOUSE, curses.KEY_MOUSE, ord("q")))
            def keypad(self, _): pass
            def timeout(self, _): pass
            def getch(self): return next(self.keys)

        def slow_read(_):
            preview_block.wait(1)
            return []

        with (patch("desk.curses.curs_set"), patch("desk.curses.has_colors", return_value=False),
              patch("desk.curses.mousemask"), patch("desk.curses.mouseinterval"),
              patch("desk.curses.getmouse", side_effect=[(0, 73, 17, 0, curses.BUTTON1_PRESSED)] * 2),
              patch("desk.load_snapshot", return_value=snapshot), patch("desk.load_pins", return_value=set()),
              patch("desk.watch_events"), patch("desk.draw_cards"),
              patch("desk.read_agent", side_effect=slow_read),
              patch("desk.call") as command):
            try:
                desk(InputScreen(height=74))
            finally:
                preview_block.set()
        command.assert_called_once_with("workspace", "focus", "w2")

    def run_shortcuts(self, keys, answers=()):
        snapshot = {"workspaces": [
            {"workspace_id": "w1", "label": "first", "worktree": {"repo_key": "r", "repo_name": "alpha", "is_linked_worktree": True}},
            {"workspace_id": "w2", "label": "second", "worktree": {"repo_key": "r", "repo_name": "alpha", "is_linked_worktree": True}},
            {"workspace_id": "w3", "label": "third", "worktree": {"repo_key": "s", "repo_name": "zeta"}}],
            "agents": [{"workspace_id": "w1", "pane_id": f"w1:p{i}", "agent_status": "working" if i == 1 else "idle",
                        "name": f"agent-{i}"} for i in range(1, 5)]}

        class InputScreen(Screen):
            def __init__(self):
                super().__init__(height=74)
                self.keys = iter((*keys, ord("q")))
            def keypad(self, _): pass
            def timeout(self, _): pass
            def getch(self): return next(self.keys)

        with (patch("desk.curses.curs_set"), patch("desk.curses.has_colors", return_value=False),
              patch("desk.curses.mousemask"), patch("desk.curses.mouseinterval"),
              patch("desk.load_snapshot", return_value=snapshot) as load,
              patch("desk.load_pins", return_value=set()), patch("desk.save_pins") as save,
              patch("desk.load_closed_worktrees", return_value=[]), patch("desk.watch_events"),
              patch("desk.fetch_preview"), patch("desk.draw_cards") as render,
              patch("desk.ask", side_effect=list(answers)) as ask_prompt,
              patch("desk.create_worktree", return_value="w4") as create,
              patch("desk.cleanup_action") as cleanup_action_mock,
              patch("desk.call") as command):
            desk(InputScreen())
        return snapshot, load, save, render, ask_prompt, create, cleanup_action_mock, command

    def test_filter_shortcuts_accept_both_cases(self):
        for key in ("f", "F"):
            with self.subTest(key=key):
                result = self.run_shortcuts((ord(key),))
                frame = result[3].call_args_list[-1].args
                self.assertEqual(frame[6], "active")
                self.assertEqual([item[0]["workspace_id"] for item in frame[2]], ["w1"])
        frame = self.run_shortcuts((ord("f"), ord("f")))[3].call_args_list[-1].args
        self.assertEqual(frame[6], "empty")

    def test_pin_fold_and_new_shortcuts(self):
        for key in ("p", "P"):
            with self.subTest(key=key):
                self.run_shortcuts((ord(key),))[2].assert_called_once_with({"w1"})
        for key in ("g", "G"):
            with self.subTest(key=key):
                frame = self.run_shortcuts((ord(key),))[3].call_args_list[-1].args
                self.assertEqual([item[0]["workspace_id"] for item in frame[2]], ["group:r", "w3"])
        for key in ("n", "N"):
            with self.subTest(key=key):
                result = self.run_shortcuts((ord(key),), ("feature", "main", "Feature"))
                result[5].assert_called_once_with(result[0]["workspaces"][0], "feature", "main", "Feature")
                self.assertEqual(result[4].call_count, 3)

    def test_cleanup_shortcuts_and_context_hint(self):
        for key, action in (("d", "close"), ("D", "close"), ("x", "remove"), ("X", "remove")):
            with self.subTest(key=key):
                result = self.run_shortcuts((ord("c"), ord(key)))
                result[6].assert_called_once_with(result[3].call_args_list[-1].args[0], result[0]["workspaces"][1], action)
        for key in ("d", "x"):
            with self.subTest(key=key):
                result = self.run_shortcuts((ord(key),))
                result[6].assert_not_called()
                self.assertIn("Press C to enter Cleanup", result[3].call_args_list[-1].args[5])

    def test_contextual_shortcuts_explain_when_unavailable(self):
        for keys, hint in (((ord("j"), ord("g")), "only one workspace"),
                           ((ord("f"), ord("g")), "All view"),
                           ((ord("c"), ord("f")), "return to the board"),
                           ((ord("c"), ord("p")), "Return to the board"),
                           ((ord("c"), ord("g")), "Return to the board"),
                           ((ord("c"), ord("n")), "Return to the board"),
                           ((ord("c"), ord("a")), "No agents to preview"),
                           ((ord("c"), ord("v")), "No agent to resume"),
                           ((ord("c"), ord("1")), "No agent 1")):
            with self.subTest(keys=keys):
                frame = self.run_shortcuts(keys)[3].call_args_list[-1].args
                self.assertIn(hint, frame[5])

    def test_agent_shortcuts(self):
        for key in ("a", "A"):
            with self.subTest(key=key):
                frame = self.run_shortcuts((ord(key),))[3].call_args_list[-1].args
                self.assertEqual(frame[10], "agent-2")
        for key in ("v", "V"):
            with self.subTest(key=key):
                self.run_shortcuts((ord("a"), ord(key)))[7].assert_called_once_with("agent", "focus", "w1:p2")
        for number in range(1, 5):
            with self.subTest(number=number):
                self.run_shortcuts((ord(str(number)),))[7].assert_called_once_with("agent", "focus", f"w1:p{number}")

    def test_open_navigation_search_refresh_and_quit_shortcuts(self):
        for keys, workspace_id in (((10,), "w1"), ((ord("l"), 10), "w2"),
                                    ((curses.KEY_RIGHT, 10), "w2"), ((ord("j"), 10), "w3"),
                                    ((ord("l"), ord("h"), 13), "w1"),
                                    ((ord("j"), ord("k"), curses.KEY_ENTER), "w1"),
                                    ((curses.KEY_RIGHT, curses.KEY_LEFT, 10), "w1"),
                                    ((curses.KEY_DOWN, curses.KEY_UP, 10), "w1")):
            with self.subTest(keys=keys):
                self.run_shortcuts(keys)[7].assert_called_once_with("workspace", "focus", workspace_id)
        frame = self.run_shortcuts((ord("/"),), ("second",))[3].call_args_list[-1].args
        self.assertEqual([item[0]["workspace_id"] for item in frame[2]], ["w2"])
        self.assertEqual(self.run_shortcuts((ord("R"),))[1].call_count, 2)
        frame = self.run_shortcuts((ord("C"), ord("C")))[3].call_args_list[-1].args
        self.assertFalse(frame[9])
        self.assertEqual(self.run_shortcuts((ord("Q"),))[3].call_count, 1)

    def test_group_filter_and_pin_order(self):
        snapshot = {"workspaces": [
            {"workspace_id": "a", "label": "alpha", "worktree": {"repo_key": "r", "repo_name": "repo"}},
            {"workspace_id": "b", "label": "beta", "worktree": {"repo_key": "r", "repo_name": "repo"}},
            {"workspace_id": "c", "label": "other", "worktree": {"repo_key": "s", "repo_name": "other"}}],
            "agents": [{"workspace_id": "b", "pane_id": "b:p1", "agent_status": "blocked", "name": "fix"}]}
        self.assertEqual([a[0]["workspace_id"] for a in rows(snapshot, mode="active")], ["b"])
        self.assertEqual([a[0]["workspace_id"] for a in rows(snapshot, mode="empty")], ["c", "a"])
        self.assertEqual(rows(snapshot, pins={"a"})[0][0]["workspace_id"], "a")
        self.assertEqual([a[0]["workspace_id"] for a in rows(snapshot, collapsed={"r"})], ["c", "group:r"])
        self.assertEqual(attention(snapshot)[0]["pane_id"], "b:p1")

    def test_pins_persist_in_plugin_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": directory}):
            save_pins({"w2", "w1"})
            self.assertEqual(load_pins(), {"w1", "w2"})

    @patch("desk.subprocess.run")
    @patch("desk.call")
    def test_create_preview_and_cleanup_commands(self, command, process):
        command.side_effect = [
            {"result": {"workspace": {"workspace_id": "w2"}}},
            {"result": {"type": "worktree_list", "worktrees": [
                {"path": "/repo/closed", "label": "repo", "is_linked_worktree": True, "open_workspace_id": None}]}}
        ]
        process.return_value = subprocess.CompletedProcess([], 0, "old\nrecent answer\n", "")
        workspace = {"workspace_id": "w1", "label": "repo", "worktree": {"repo_key": "r", "repo_name": "repo", "repo_root": "/repo", "checkout_path": "/repo"}}
        self.assertEqual(create_worktree(workspace, "feature", "main", "Feature"), "w2")
        command.assert_any_call("worktree", "create", "--workspace", "w1", "--branch", "feature", "--focus", "--base", "main", "--label", "Feature")
        self.assertEqual(read_agent("w1:p1"), ["old", "recent answer"])
        closed = load_closed_worktrees({"workspaces": [workspace]})
        self.assertEqual(closed[0]["worktree"]["checkout_path"], "/repo/closed")
        self.assertEqual(closed[0]["label"], "closed")
        self.assertEqual(len(cleanup_items({"workspaces": [workspace], "agents": []}, closed)), 1)

    @patch("desk.ask", return_value="REMOVE")
    @patch("desk.call")
    def test_cleanup_requires_confirmation_and_uses_herdr_remove(self, command, _ask):
        workspace = {"workspace_id": "w1", "label": "old", "worktree": {"checkout_path": "/repo/old", "is_linked_worktree": True}}
        cleanup_action(None, workspace, "remove")
        command.assert_called_once_with("worktree", "remove", "--workspace", "w1")

    @patch("desk.ask", return_value="")
    @patch("desk.call")
    def test_cleanup_cancel_does_not_remove(self, command, _ask):
        workspace = {"workspace_id": "w1", "label": "old", "worktree": {"checkout_path": "/repo/old", "is_linked_worktree": True}}
        cleanup_action(None, workspace, "remove")
        command.assert_not_called()

    @patch("desk.ask", return_value="CLOSE")
    @patch("desk.call")
    def test_cleanup_close_uses_herdr_workspace_command(self, command, _ask):
        workspace = {"workspace_id": "w1", "label": "old", "worktree": {"checkout_path": "/repo/old", "is_linked_worktree": True}}
        cleanup_action(None, workspace, "close")
        command.assert_called_once_with("workspace", "close", "w1")

    @patch("desk.call")
    def test_closed_checkout_reopens_through_herdr(self, command):
        command.return_value = {"result": {"workspace": {"workspace_id": "w2"}}}
        closed = {"workspace_id": "closed:/repo/old", "label": "old", "closed_checkout": True,
                  "worktree": {"repo_root": "/repo", "checkout_path": "/repo/old"}}
        self.assertEqual(cleanup_action(None, closed, "open"), "w2")
        command.assert_called_once_with("worktree", "open", "--cwd", "/repo", "--path", "/repo/old", "--focus")

    def test_event_subscription_wakes_desk(self):
        sent = []
        stopped = threading.Event()
        class Connection:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def settimeout(self, _): pass
            def connect(self, path): self.path = path
            def sendall(self, data): sent.append(json.loads(data))
            def makefile(self, _): return io.StringIO('{"id":"herdr-atlas","result":{"type":"ok"}}\n{"event":"pane.agent_status_changed","data":{}}\n')
        class Changed:
            def set(self): stopped.set()
        with patch("desk.socket.socket", return_value=Connection()):
            watch_events(Changed(), stopped, "/tmp/fake.sock")
        self.assertEqual(sent[0]["method"], "events.subscribe")


if __name__ == "__main__":
    unittest.main()
