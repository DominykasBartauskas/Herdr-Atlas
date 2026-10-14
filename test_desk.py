import unittest
import tempfile
import os
import json
import threading
import io
import subprocess
from unittest.mock import patch

from desk import (agent_label, attention, cleanup_action, cleanup_items, create_worktree,
                  draw_cards, load_closed_worktrees, load_pins, load_snapshot,
                  mouse_target, read_agent, rows, save_pins, watch_events)


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
