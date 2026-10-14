#!/usr/bin/env python3
"""Live Herdr control center. Uses only Python's standard library."""

import curses
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time


HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
PRIORITY = {"blocked": 0, "done": 1, "working": 2, "idle": 3, "unknown": 4}
EVENTS = ("workspace.created", "workspace.updated", "workspace.renamed", "workspace.closed",
          "worktree.created", "worktree.opened", "worktree.removed", "pane.created",
          "pane.closed", "pane.updated", "pane.agent_detected", "pane.agent_status_changed")


def call(*args):
    result = subprocess.run([HERDR, *args], text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Herdr command failed")
    return json.loads(result.stdout)


def load_snapshot():
    payload = call("api", "snapshot")
    snapshot = payload.get("result", {}).get("snapshot")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("workspaces"), list):
        raise ValueError("Unexpected Herdr snapshot response")
    return snapshot


def watch_events(changed, stopped, socket_path=None):
    """Wake the desk on Herdr changes; polling remains the reconnect fallback."""
    path = socket_path or os.environ.get("HERDR_SOCKET_PATH")
    if not path:
        return
    request = {"id": "herdr-atlas", "method": "events.subscribe",
               "params": {"subscriptions": [{"type": name} for name in EVENTS]}}
    while not stopped.is_set():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect(path)
                connection.sendall((json.dumps(request) + "\n").encode())
                with connection.makefile("r") as stream:
                    for line in stream:
                        if stopped.is_set():
                            return
                        payload = json.loads(line)
                        if payload.get("id") != "herdr-atlas":
                            changed.set()
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        stopped.wait(2)


def state_path():
    root = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    return Path(root) / "desk.json" if root else None


def load_pins():
    path = state_path()
    if not path:
        return set()
    try:
        return set(json.loads(path.read_text()).get("pins", []))
    except (OSError, ValueError, TypeError):
        return set()


def save_pins(pins):
    path = state_path()
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pins": sorted(pins)}))


def repo_key(workspace):
    worktree = workspace.get("worktree") or {}
    return worktree.get("repo_key") or worktree.get("repo_root") or workspace.get("workspace_id")


def repo_name(workspace):
    return (workspace.get("worktree") or {}).get("repo_name") or workspace.get("label") or "Workspace"


def attention(snapshot):
    workspaces = {w["workspace_id"]: w for w in snapshot["workspaces"]}
    return sorted((a for a in snapshot.get("agents", []) if a.get("agent_status") in ("blocked", "done")),
                  key=lambda a: (PRIORITY[a["agent_status"]], repo_name(workspaces.get(a["workspace_id"], {})), agent_label(a, snapshot)))


def create_worktree(workspace, branch, base="", label=""):
    if not branch.strip():
        raise ValueError("Branch is required")
    args = ["worktree", "create", "--workspace", workspace["workspace_id"], "--branch", branch.strip(), "--focus"]
    if base.strip():
        args.extend(("--base", base.strip()))
    if label.strip():
        args.extend(("--label", label.strip()))
    return call(*args)["result"]["workspace"]["workspace_id"]


def read_agent(pane_id):
    result = subprocess.run([HERDR, "agent", "read", pane_id, "--source", "recent", "--lines", "8"],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Agent read failed")
    return [line.rstrip() for line in result.stdout.splitlines() if line.strip()][-3:]


def load_closed_worktrees(snapshot):
    closed = []
    seen = set()
    for workspace in snapshot["workspaces"]:
        worktree = workspace.get("worktree") or {}
        key = worktree.get("repo_key")
        if not key or key in seen:
            continue
        seen.add(key)
        result = call("worktree", "list", "--workspace", workspace["workspace_id"])["result"]
        for checkout in result.get("worktrees", []):
            if checkout.get("is_linked_worktree") and not checkout.get("open_workspace_id"):
                closed.append({"workspace_id": "closed:" + checkout["path"], "label": Path(checkout["path"]).name or checkout["path"],
                               "worktree": {"repo_key": key, "repo_name": worktree.get("repo_name"), "repo_root": worktree.get("repo_root"), "checkout_path": checkout["path"], "is_linked_worktree": True},
                               "closed_checkout": True, "branch": checkout.get("branch")})
    return closed


def cleanup_items(snapshot, closed):
    busy = {a["workspace_id"] for a in snapshot.get("agents", [])}
    open_idle = [(w, []) for w in snapshot["workspaces"] if (w.get("worktree") or {}).get("is_linked_worktree") and w["workspace_id"] not in busy]
    return open_idle + [(w, []) for w in closed]


def agent_label(agent, snapshot):
    pane = next((p for p in snapshot.get("panes", []) if p["pane_id"] == agent.get("pane_id")), {})
    tab = next((t for t in snapshot.get("tabs", []) if t["tab_id"] == agent.get("tab_id")), {})
    for value in (agent.get("name"), pane.get("label"), agent.get("title"),
                  pane.get("title"), agent.get("terminal_title_stripped"),
                  pane.get("terminal_title_stripped"), tab.get("label"),
                  agent.get("display_agent"), agent.get("agent")):
        if value and value != agent.get("pane_id"):
            return str(value)
    return "Unnamed agent"


def rows(snapshot, query="", mode="all", pins=None, collapsed=None):
    pins = pins or set()
    collapsed = collapsed or set()
    workspaces = snapshot["workspaces"]
    agents = snapshot.get("agents", [])
    by_workspace = {}
    for agent in agents:
        by_workspace.setdefault(agent["workspace_id"], []).append(agent)

    output = []
    ordered = sorted(enumerate(workspaces), key=lambda pair: (
        pair[1]["workspace_id"] not in pins,
        repo_name(pair[1]).lower(),
        min((PRIORITY.get(a.get("agent_status"), 9) for a in by_workspace.get(pair[1]["workspace_id"], [])), default=9), pair[0]))
    for _, workspace in ordered:
        workspace_id = workspace["workspace_id"]
        name = workspace.get("label") or workspace_id
        worktree = workspace.get("worktree") or {}
        members = sorted(by_workspace.get(workspace_id, []), key=lambda a: (PRIORITY.get(a.get("agent_status"), 9), agent_label(a, snapshot)))
        if mode == "active" and not any(a.get("agent_status") in ("blocked", "done", "working") for a in members):
            continue
        if mode == "empty" and members:
            continue
        searchable = " ".join([name, worktree.get("repo_name") or "", worktree.get("checkout_path") or ""] + [" ".join((agent_label(a, snapshot), a.get("agent_status") or "")) for a in members]).lower()
        if query and query.lower() not in searchable:
            continue
        output.append((workspace, members))
    if mode == "all" and not query:
        grouped = {}
        for workspace, members in output:
            grouped.setdefault(repo_key(workspace), []).append((workspace, members))
        folded = []
        emitted = set()
        for workspace, members in output:
            key = repo_key(workspace)
            if key in collapsed and key not in emitted and len(grouped[key]) > 1:
                folded.append(({"workspace_id": "group:" + key, "label": repo_name(workspace),
                                "worktree": workspace.get("worktree"), "collapsed_group": key,
                                "group_count": len(grouped[key])}, []))
                emitted.add(key)
            elif key not in collapsed:
                folded.append((workspace, members))
        output = folded
    return output


def put(screen, y, x, value, width, attr=0):
    height, columns = screen.getmaxyx()
    width = min(width, columns - x)
    if y < 0 or y >= height or x >= columns or width <= 0:
        return
    value = str(value).replace("\n", " ")
    if len(value) > width:
        value = value[:width - 1] + "…" if width > 1 else value[:width]
    try:
        screen.addstr(y, x, value, attr)
    except curses.error:
        pass


def status_style(status, selected=False):
    pair = {"blocked": (16, 17), "done": (18, 19), "working": (14, 15),
            "idle": (12, 13), "unknown": (20, 21), "empty": (20, 21)}.get(status, (20, 21))
    return curses.color_pair(pair[bool(selected)]) | (curses.A_BOLD if status in ("blocked", "done", "working") else 0)


def surface(screen, y, x, width, height, selected=False):
    background = curses.color_pair(11 if selected else 10)
    for line in range(height):
        put(screen, y + line, x, " " * width, width, background)
        if selected:
            put(screen, y + line, x, "▌", 1, curses.color_pair(15))


def geometry(height, width, selected):
    left = max(3, (width - 128) // 2)
    content_width = width - 2 * left
    columns = 2 if content_width >= 100 else 1
    card_width = (content_width - 1) // 2 if columns == 2 else content_width
    grid_y = 16 if height >= 50 else 12
    detail_y = height - (16 if height >= 50 else 12)
    visible_rows = max(1, (detail_y - grid_y) // 6)
    scroll_row = max(0, selected // columns - visible_rows + 1)
    return left, content_width, columns, card_width, grid_y, visible_rows, scroll_row, detail_y


def mouse_target(height, width, selected, item_count, member_count, x, y, attention_count=0, cleanup=False):
    left, content_width, columns, card_width, grid_y, visible_rows, scroll_row, detail_y = geometry(height, width, selected)
    if height >= 50 and 10 <= y <= 11 and left <= x < left + content_width:
        index = (y - 10) * 2 + (x >= left + content_width // 2)
        if index < attention_count:
            return "attention", index
    if y == grid_y - 2 and content_width >= 100:
        if left + content_width - 23 <= x < left + content_width - 13:
            return "new", None
        if left + content_width - 12 <= x < left + content_width:
            return "cleanup", None
        if left + 22 <= x < left + 26:
            return "filter", 0
        if left + 27 <= x < left + 34:
            return "filter", 1
        if left + 35 <= x < left + 45:
            return "filter", 2
    if content_width >= 100 and y == detail_y + 1 and left + content_width - 22 <= x < left + content_width - 2:
        return ("remove" if cleanup else "open"), None
    if content_width >= 100 and y == detail_y + 1 and left + content_width - 36 <= x < left + content_width - 23:
        return ("close" if cleanup else "pin"), None
    if height >= 50 and content_width >= 100 and y == detail_y + 9 and member_count and left + content_width - 22 <= x < left + content_width - 2:
        return "resume", None
    if detail_y + 6 <= y <= detail_y + 7 and left + 3 <= x < left + content_width - 3:
        column = 0 if x < left + content_width // 2 else 1
        index = (y - detail_y - 6) * 2 + column
        if index < member_count:
            return "agent", index
    if grid_y <= y < grid_y + visible_rows * 6 and (y - grid_y) % 6 < 5:
        column = (x - left) // (card_width + 1)
        if 0 <= column < columns and left + column * (card_width + 1) <= x < left + column * (card_width + 1) + card_width:
            index = (scroll_row + (y - grid_y) // 6) * columns + column
            if index < item_count:
                return "card", index
    return None, None


def draw_cards(screen, snapshot, items, selected, query, message, mode="all", pins=None, preview=None, cleanup=False, preview_name=""):
    screen.erase()
    height, width = screen.getmaxyx()
    if height < 30 or width < 48:
        put(screen, 1, 2, "Herdr Atlas needs at least 48 columns and 30 rows", width - 4)
        screen.refresh()
        return

    left, content_width, columns, card_width, grid_y, visible_rows, scroll_row, detail_y = geometry(height, width, selected)
    agents = snapshot.get("agents", [])
    counts = {state: sum(a.get("agent_status") == state for a in agents) for state in PRIORITY}
    put(screen, 1, left, "◆  HERDR", 18, curses.color_pair(6) | curses.A_BOLD)
    put(screen, 1, left + 17, "Control center", 24, curses.A_BOLD)
    if width >= 70:
        put(screen, 1, left + content_width - 17, "●  LIVE SESSION", 16, curses.color_pair(3))
    put(screen, 2, left, "A clear view of everything in motion.", content_width, curses.color_pair(7))

    summary = [
        ("NEEDS YOU", counts["blocked"], 1),
        ("FINISHED", counts["done"], 2),
        ("WORKING", counts["working"], 3),
        ("WORKSPACES", len(snapshot["workspaces"]), 6),
    ]
    if width < 80:
        put(screen, 6, left, "    ·    ".join(f"{count} {label.lower()}" for label, count, _ in summary),
            content_width, curses.A_BOLD)
    else:
        summary_width = (content_width - 3) // 4
        for index, (label, count, color) in enumerate(summary):
            x = left + index * (summary_width + 1)
            surface(screen, 5, x, summary_width, 4)
            color_pair = {1: 16, 2: 18, 3: 14, 6: 14}[color]
            put(screen, 6, x + 2, f'{count:02}', 5, curses.color_pair(color_pair) | curses.A_BOLD)
            put(screen, 7, x + 2, label, summary_width - 4, curses.color_pair(12))

    if height >= 50:
        urgent = attention(snapshot)
        put(screen, 9, left, f"ATTENTION  {len(urgent)}", content_width, curses.color_pair(1) | curses.A_BOLD)
        for index, agent in enumerate(urgent[:4]):
            y, column = 10 + index // 2, index % 2
            x = left + column * (content_width // 2)
            status = "● NEEDS YOU" if agent.get("agent_status") == "blocked" else "✓ FINISHED"
            put(screen, y, x, f'{status}  {agent_label(agent, snapshot)}', content_width // 2 - 2,
                status_style(agent.get("agent_status")))
        put(screen, 14, left, "CLEANUP" if cleanup else "WORKSPACE BOARD", 19, curses.A_BOLD)
    else:
        put(screen, 10, left, "CLEANUP" if cleanup else "WORKSPACE BOARD", 19, curses.A_BOLD)
    heading_y = grid_y - 2
    if content_width >= 100:
        if not cleanup:
            put(screen, heading_y, left + 22, "ALL  ACTIVE  NO AGENT", 25, curses.color_pair(7))
            put(screen, heading_y, left + 22 + {"all": 0, "active": 5, "empty": 13}.get(mode, 0),
                {"all": "ALL", "active": "ACTIVE", "empty": "NO AGENT"}.get(mode, "ALL"), 9, curses.color_pair(3) | curses.A_BOLD)
            put(screen, heading_y, left + content_width - 23, "+ NEW", 9, curses.color_pair(3) | curses.A_BOLD)
        put(screen, heading_y, left + content_width - 12, "CLEANUP" if not cleanup else "BOARD", 12, curses.color_pair(3) | curses.A_BOLD)
    else:
        put(screen, heading_y, left + 22, f'{mode.upper()}  ·  F filter  N new  C cleanup', content_width - 22, curses.color_pair(7))
    card_height = 6
    for item_index, (workspace, members) in enumerate(items):
        grid_row, column = divmod(item_index, columns)
        if not scroll_row <= grid_row < scroll_row + visible_rows:
            continue
        y = grid_y + (grid_row - scroll_row) * card_height
        x = left + column * (card_width + 1)
        chosen = item_index == selected
        surface(screen, y, x, card_width, 5, chosen)
        if workspace.get("collapsed_group"):
            put(screen, y + 1, x + 3, "▸ REPOSITORY", 16, curses.color_pair(15) | curses.A_BOLD)
            put(screen, y + 1, x + 18, workspace["label"], card_width - 20, curses.A_BOLD)
            put(screen, y + 2, x + 3, f'{workspace["group_count"]} workspaces hidden  ·  click to expand', card_width - 5, curses.color_pair(12))
            continue
        status = min((a.get("agent_status") or "unknown" for a in members),
                     key=lambda state: PRIORITY.get(state, 9), default="empty")
        status_text = {"blocked": "● NEEDS YOU", "done": "✓ FINISHED", "working": "● WORKING",
                       "idle": "○ IDLE", "unknown": "○ UNKNOWN", "empty": "· NO AGENT"}[status]
        if workspace.get("closed_checkout"):
            status_text = "○ CLOSED"
        put(screen, y + 1, x + 3, status_text, 14, status_style(status, chosen))
        starred = "★ " if workspace["workspace_id"] in (pins or set()) else ""
        put(screen, y + 1, x + 17, starred + (workspace.get("label") or workspace["workspace_id"]), card_width - 19,
            curses.A_BOLD | curses.color_pair(11 if chosen else 10))
        worktree = workspace.get("worktree") or {}
        repo = worktree.get("repo_name") or "Workspace"
        kind = "closed checkout" if workspace.get("closed_checkout") else "worktree" if worktree.get("is_linked_worktree") else "project"
        muted = curses.color_pair(13 if chosen else 12)
        put(screen, y + 2, x + 3, f'{repo}  ·  {kind}', card_width - 5, muted)
        names = [agent_label(a, snapshot) for a in members]
        agents_text = f'{len(members)} agent' + ("s" if len(members) != 1 else "")
        if names:
            agents_text += "  ·  " + ", ".join(names[:2])
            if len(names) > 2:
                agents_text += f' +{len(names) - 2}'
        put(screen, y + 3, x + 3, agents_text, card_width - 5, muted)

    above = scroll_row * columns
    below = max(0, len(items) - (scroll_row + visible_rows) * columns)
    if above or below:
        hint = "   ".join(part for part in (f"↑ {above} above" if above else "",
                                            f"↓ {below} below" if below else "") if part)
        put(screen, detail_y - 1, left, hint, content_width, curses.color_pair(7))
    surface(screen, detail_y, left, content_width, 13 if height >= 50 else 9, True)
    if items:
        workspace, members = items[selected]
        worktree = workspace.get("worktree") or {}
        put(screen, detail_y + 1, left + 3, "SELECTED", 12, curses.color_pair(15) | curses.A_BOLD)
        put(screen, detail_y + 1, left + 15, workspace.get("label") or workspace["workspace_id"],
            content_width - (54 if content_width >= 100 else 18),
            curses.color_pair(11) | curses.A_BOLD)
        if content_width >= 100 and not workspace.get("closed_checkout"):
            put(screen, detail_y + 1, left + content_width - 35, "D CLOSE" if cleanup else "☆ PIN", 12,
                curses.color_pair(15) | curses.A_BOLD)
        right_action = "↗ OPEN CHECKOUT" if workspace.get("closed_checkout") else "X REMOVE" if cleanup else "▾ EXPAND GROUP" if workspace.get("collapsed_group") else "↗ OPEN WORKSPACE"
        if content_width >= 100:
            put(screen, detail_y + 1, left + content_width - 21, right_action, 18,
                curses.color_pair(15) | curses.A_BOLD)
        tabs = workspace.get("tab_count", 0)
        panes = workspace.get("pane_count", 0)
        detail = (f'{worktree.get("repo_name") or "Workspace"}  ·  branch {workspace.get("branch") or "?"}  ·  closed checkout'
                  if workspace.get("closed_checkout") else
                  f'{worktree.get("repo_name") or "Workspace"}  ·  {tabs} tab{"s" if tabs != 1 else ""}  ·  {panes} pane{"s" if panes != 1 else ""}')
        put(screen, detail_y + 2, left + 3, detail, content_width - 6, curses.color_pair(13))
        if worktree.get("checkout_path"):
            put(screen, detail_y + 3, left + 3, worktree["checkout_path"], content_width - 6, curses.color_pair(13))
        put(screen, detail_y + 5, left + 3, "AGENTS", 12, curses.color_pair(11) | curses.A_BOLD)
        if workspace.get("collapsed_group"):
            put(screen, detail_y + 6, left + 3, "Press Enter or click the card to expand this repository", content_width - 6, curses.color_pair(13))
        elif not members:
            put(screen, detail_y + 6, left + 3, "No live agents in this workspace", content_width - 6, curses.color_pair(13))
        for index, agent in enumerate(members[:4], 1):
            x = left + 3 + ((index - 1) % 2) * ((content_width - 6) // 2)
            y = detail_y + 6 + (index - 1) // 2
            status = agent.get("agent_status") or "unknown"
            put(screen, y, x, f'{index}  {status.upper():8}  {agent_label(agent, snapshot)}', (content_width - 8) // 2,
                status_style(status, True))
        if height >= 50:
            put(screen, detail_y + 9, left + 3, "RECENT OUTPUT" + ("  ·  " + preview_name if preview_name else ""), content_width - 6, curses.color_pair(11) | curses.A_BOLD)
            if members and content_width >= 100:
                put(screen, detail_y + 9, left + content_width - 21, "↗ RESUME AGENT", 18,
                    curses.color_pair(15) | curses.A_BOLD)
            for offset, line in enumerate((preview or [])[-3:]):
                put(screen, detail_y + 10 + offset, left + 3, line, content_width - 6, curses.color_pair(13))
    else:
        put(screen, detail_y + 2, left + 3, "No linked checkouts without agents" if cleanup else "No workspaces match this search", content_width - 6, curses.color_pair(13))

    put(screen, height - 2, left, message or "", content_width, curses.color_pair(8))
    put(screen, height - 1, 0,
        "  Click select · Double-click open · F Filter · P Pin · G Fold · N New · C Cleanup · D Close · X Remove · A Preview · V Resume · Q Quit".ljust(width),
        width, curses.A_REVERSE)
    screen.refresh()


def ask(screen, label):
    height, width = screen.getmaxyx()
    left = max(3, (width - 128) // 2)
    label = label[:max(1, width - left - 4)]
    screen.timeout(-1)
    curses.echo()
    curses.curs_set(1)
    try:
        screen.move(height - 2, left)
        screen.clrtoeol()
        put(screen, height - 2, left, label, width - left)
        return screen.getstr(height - 2, left + len(label), max(1, width - left - len(label) - 2)).decode(errors="replace").strip()
    finally:
        curses.noecho()
        curses.curs_set(0)
        screen.timeout(250)


def cleanup_action(screen, workspace, action):
    worktree = workspace.get("worktree") or {}
    checkout = worktree.get("checkout_path") or ""
    if workspace.get("closed_checkout"):
        if action == "open":
            result = call("worktree", "open", "--cwd", worktree["repo_root"], "--path", checkout, "--focus")
            return result["result"]["workspace"]["workspace_id"]
        raise ValueError("This checkout is already closed; open it first")
    if action == "open":
        call("workspace", "focus", workspace["workspace_id"])
        return workspace["workspace_id"]
    if action not in ("close", "remove"):
        return None
    if ask(screen, f'Type {action.upper()} to {action} selected checkout: ') != action.upper():
        return None
    if action == "close":
        call("workspace", "close", workspace["workspace_id"])
    else:
        call("worktree", "remove", "--workspace", workspace["workspace_id"])
    return None


def desk(screen):
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        palette = ((curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_CYAN,
                    curses.COLOR_WHITE, curses.COLOR_MAGENTA, curses.COLOR_CYAN,
                    curses.COLOR_WHITE, curses.COLOR_YELLOW, curses.COLOR_BLUE)
                   if curses.COLORS < 256 else (203, 114, 80, 250, 177, 117, 245, 222, 240))
        for number, color in enumerate(palette, 1):
            curses.init_pair(number, color, background)
        if curses.COLORS >= 256:
            panels = ((252, 236), (255, 238), (247, 236), (252, 238),
                      (80, 236), (80, 238), (203, 236), (203, 238),
                      (114, 236), (114, 238), (177, 236), (177, 238))
        else:
            panels = ((curses.COLOR_WHITE, curses.COLOR_BLACK),) * 4 + (
                (curses.COLOR_CYAN, curses.COLOR_BLACK),) * 2 + (
                (curses.COLOR_RED, curses.COLOR_BLACK),) * 2 + (
                (curses.COLOR_GREEN, curses.COLOR_BLACK),) * 2 + (
                (curses.COLOR_MAGENTA, curses.COLOR_BLACK),) * 2
        for number, (foreground, panel_background) in enumerate(panels, 10):
            curses.init_pair(number, foreground, panel_background)
    screen.keypad(True)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.mouseinterval(0)
    screen.timeout(250)
    snapshot = load_snapshot()
    changed = threading.Event()
    stopped = threading.Event()
    threading.Thread(target=watch_events, args=(changed, stopped), daemon=True).start()
    last_refresh = time.monotonic()
    query = ""
    mode = "all"
    cleanup = False
    closed = []
    pins = load_pins()
    collapsed = set()
    selected = 0
    preview_id = None
    preview_lines = []
    preview_index = 0
    message = ""
    last_card_press = (None, 0.0)
    last_mouse_press = (None, None, 0.0)
    redraw = True
    try:
        while True:
            items = cleanup_items(snapshot, closed) if cleanup else rows(snapshot, query, mode, pins, collapsed)
            selected = min(selected, max(0, len(items) - 1))
            workspace, members = items[selected] if items else ({}, [])
            pane_id = members[preview_index % len(members)]["pane_id"] if members else None
            if pane_id != preview_id:
                preview_id = pane_id
                try:
                    preview_lines = read_agent(pane_id) if pane_id else []
                except (RuntimeError, ValueError, json.JSONDecodeError) as error:
                    preview_lines = [str(error)]
            if redraw:
                draw_cards(screen, snapshot, items, selected, query, message, mode, pins, preview_lines, cleanup,
                           agent_label(members[preview_index % len(members)], snapshot) if members else "")
                redraw = False
            key = screen.getch()
            message = ""
            refresh = changed.is_set() or time.monotonic() - last_refresh >= 5
            changed.clear()
            if key == -1 and not refresh:
                continue
            if key in (ord("q"), 27):
                return
            redraw = True
            screen_height, screen_width = screen.getmaxyx()
            columns = geometry(screen_height, screen_width, selected)[2]
            command, index = None, None
            if key == curses.KEY_MOUSE:
                try:
                    _, x, y, _, state = curses.getmouse()
                except curses.error:
                    selected = min(selected + columns, max(0, len(items) - 1))
                    continue
                wheel_up = getattr(curses, "BUTTON4_PRESSED", 0)
                wheel_down = getattr(curses, "BUTTON5_PRESSED", wheel_up << 6)
                if state & wheel_up:
                    selected = max(0, selected - columns)
                    continue
                if state & wheel_down:
                    selected = min(selected + columns, max(0, len(items) - 1))
                    continue
                if state & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED):
                    target, index = mouse_target(screen_height, screen_width, selected, len(items), len(members), x, y,
                                                 len(attention(snapshot)), cleanup)
                    pressed = bool(state & curses.BUTTON1_PRESSED)
                    now = time.monotonic()
                    if pressed:
                        last_mouse_press = (target, index, now)
                    elif state & curses.BUTTON1_CLICKED and last_mouse_press[:2] == (target, index) and now - last_mouse_press[2] < 0.4:
                        continue
                    if target == "card":
                        selected = index
                        card = items[index][0]
                        if card.get("collapsed_group"):
                            command = "fold"
                        elif state & curses.BUTTON1_DOUBLE_CLICKED or (pressed and last_card_press[0] == index and now - last_card_press[1] < 0.4):
                            command = "open"
                        if pressed:
                            last_card_press = (index, now)
                    else:
                        command = target
                        last_card_press = (None, 0.0)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(selected + columns, max(0, len(items) - 1))
            elif key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - columns)
            elif key in (curses.KEY_RIGHT, ord("l")):
                selected = min(selected + 1, max(0, len(items) - 1))
            elif key in (curses.KEY_LEFT, ord("h")):
                selected = max(0, selected - 1)
            elif key in (10, 13, curses.KEY_ENTER):
                command = "open"
            elif ord("1") <= key <= ord("4"):
                command, index = "agent", key - ord("1")
            elif key == ord("a"):
                preview_index += 1
            elif key == ord("v"):
                command = "resume"
            elif key == ord("f"):
                command, index = "filter", ("all", "active", "empty").index(mode) + 1
            elif key == ord("p"):
                command = "pin"
            elif key == ord("g"):
                command = "fold"
            elif key == ord("n"):
                command = "new"
            elif key == ord("c"):
                command = "cleanup"
            elif key == ord("d"):
                command = "close"
            elif key == ord("x"):
                command = "remove"
            elif key == ord("/"):
                query = ask(screen, "Search: ")
                selected = 0
            elif key == ord("r"):
                refresh = True

            workspace, members = items[selected] if items and selected < len(items) else ({}, [])
            selected_id = workspace.get("workspace_id")
            try:
                if command == "attention" and index < len(attention(snapshot)):
                    call("agent", "focus", attention(snapshot)[index]["pane_id"])
                elif command == "filter" and not cleanup:
                    mode = ("all", "active", "empty")[index % 3]
                    selected = 0
                elif command == "cleanup":
                    if not cleanup:
                        closed = load_closed_worktrees(snapshot)
                    cleanup = not cleanup
                    selected = 0
                elif command == "new" and items and not cleanup and not workspace.get("collapsed_group"):
                    branch = ask(screen, "New branch: ")
                    if branch:
                        base = ask(screen, "Base ref (Enter for HEAD): ")
                        label = ask(screen, "Workspace label (optional): ")
                        selected_id = create_worktree(workspace, branch, base, label)
                        refresh = True
                elif command == "pin" and items and not cleanup and not workspace.get("collapsed_group"):
                    wid = workspace["workspace_id"]
                    pins.remove(wid) if wid in pins else pins.add(wid)
                    save_pins(pins)
                elif command == "fold" and items and not cleanup:
                    group = workspace.get("collapsed_group") or repo_key(workspace)
                    if group in collapsed:
                        collapsed.remove(group)
                    else:
                        collapsed.add(group)
                    selected_id = "group:" + group if group in collapsed else workspace["workspace_id"]
                elif command in ("open", "remove", "close") and items:
                    if workspace.get("collapsed_group"):
                        collapsed.discard(workspace["collapsed_group"])
                    elif cleanup:
                        action = "open" if command == "open" or workspace.get("closed_checkout") and command == "remove" else command
                        opened_id = cleanup_action(screen, workspace, action)
                        selected_id = opened_id or selected_id
                        refresh = True
                    elif command == "open":
                        call("workspace", "focus", workspace["workspace_id"])
                elif command == "agent" and index is not None and index < len(members):
                    call("agent", "focus", members[index]["pane_id"])
                elif command == "resume" and members:
                    call("agent", "focus", members[preview_index % len(members)]["pane_id"])
            except (RuntimeError, ValueError, KeyError, OSError, json.JSONDecodeError) as error:
                message = str(error)
            if refresh:
                try:
                    snapshot = load_snapshot()
                    if cleanup:
                        closed = load_closed_worktrees(snapshot)
                    last_refresh = time.monotonic()
                    preview_id = None
                except (RuntimeError, ValueError, KeyError, OSError, json.JSONDecodeError) as error:
                    message = str(error)
            updated = cleanup_items(snapshot, closed) if cleanup else rows(snapshot, query, mode, pins, collapsed)
            selected = next((i for i, item in enumerate(updated) if item[0]["workspace_id"] == selected_id),
                            min(selected, max(0, len(updated) - 1)))
    finally:
        stopped.set()


if __name__ == "__main__":
    try:
        curses.wrapper(desk)
    except (RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"Herdr Atlas: {error}", file=sys.stderr)
        sys.exit(1)
