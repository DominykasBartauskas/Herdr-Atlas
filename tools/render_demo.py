"""Render an Atlas preview from fictional data; never queries Herdr."""

import curses
from html import escape
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desk import draw_cards, rows

WIDTH, HEIGHT = 140, 66
CELL_W, CELL_H, PAD = 9, 19, 24
BACKGROUND = "#1f1f1f"
COLORS = {
    0: ("#e6e6e6", BACKGROUND), 1: ("#ff5f5f", BACKGROUND),
    2: ("#87d787", BACKGROUND), 3: ("#5fd7d7", BACKGROUND),
    6: ("#5fd7d7", BACKGROUND), 7: ("#a8a8a8", BACKGROUND),
    8: ("#ffd787", BACKGROUND), 10: ("#d0d0d0", "#303030"),
    11: ("#eeeeee", "#444444"), 12: ("#9e9e9e", "#303030"),
    13: ("#d0d0d0", "#444444"), 14: ("#5fd7d7", "#303030"),
    15: ("#5fd7d7", "#444444"), 16: ("#ff5f5f", "#303030"),
    17: ("#ff5f5f", "#444444"), 18: ("#87d787", "#303030"),
    19: ("#87d787", "#444444"), 20: ("#d787ff", "#303030"),
    21: ("#d787ff", "#444444"),
}


def workspace(repo, label, number, status=None, agent_name=None, linked=True):
    workspace_id = f"demo-{number}"
    checkout = f"/demo/worktrees/{repo}/{label}" if linked else f"/demo/repos/{repo}"
    return (
        {"workspace_id": workspace_id, "label": label, "tab_count": 1, "pane_count": 1,
         "worktree": {"repo_key": repo, "repo_name": repo, "repo_root": f"/demo/repos/{repo}",
                      "checkout_path": checkout, "is_linked_worktree": linked}},
        {"workspace_id": workspace_id, "pane_id": f"{workspace_id}:p1", "tab_id": f"{workspace_id}:t1",
         "agent_status": status, "name": agent_name} if status else None,
    )


entries = [
    workspace("demo-api", "review-endpoint", 1, "blocked", "Review API response"),
    workspace("demo-api", "cache-layer", 2, "working", "Add response cache"),
    workspace("demo-api", "auth-cleanup", 3, "idle", "Tidy auth flow"),
    workspace("demo-api", "API home", 4, linked=False),
    workspace("demo-docs", "release-notes", 5, "done", "Draft release notes"),
    workspace("demo-docs", "quickstart", 6, "working", "Refresh quickstart"),
    workspace("demo-docs", "Docs home", 7, "idle", "Organize guides", linked=False),
    workspace("demo-ui", "search-panel", 8, "idle", "Polish search panel"),
    workspace("demo-ui", "color-tokens", 9, "idle", "Update color tokens"),
    workspace("demo-ui", "UI home", 10, linked=False),
]
snapshot = {"workspaces": [item[0] for item in entries],
            "agents": [item[1] for item in entries if item[1]]}


class Screen:
    def __init__(self):
        self.cells = [[(" ", 0) for _ in range(WIDTH)] for _ in range(HEIGHT)]

    def getmaxyx(self):
        return HEIGHT, WIDTH

    def erase(self):
        self.__init__()

    def addstr(self, y, x, value, attr):
        for offset, char in enumerate(value):
            if 0 <= y < HEIGHT and 0 <= x + offset < WIDTH:
                self.cells[y][x + offset] = (char, attr)

    def refresh(self):
        pass


def style(attr):
    pair = (attr >> 8) & 255
    foreground, background = COLORS.get(pair, COLORS[0])
    if attr & curses.A_REVERSE:
        foreground, background = background, foreground
    return foreground, background, bool(attr & curses.A_BOLD)


def render(screen):
    pieces = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH * CELL_W + 2 * PAD}" '
        f'height="{HEIGHT * CELL_H + 2 * PAD}" viewBox="0 0 {WIDTH * CELL_W + 2 * PAD} {HEIGHT * CELL_H + 2 * PAD}">',
        '<title>Herdr Atlas demo with fictional workspaces and agents</title>',
        f'<rect width="100%" height="100%" rx="14" fill="{BACKGROUND}"/>',
    ]
    for y, line in enumerate(screen.cells):
        x = 0
        while x < WIDTH:
            attr = line[x][1]
            end = x + 1
            while end < WIDTH and line[end][1] == attr:
                end += 1
            foreground, background, bold = style(attr)
            if background != BACKGROUND:
                pieces.append(f'<rect x="{PAD + x * CELL_W}" y="{PAD + y * CELL_H}" '
                              f'width="{(end - x) * CELL_W}" height="{CELL_H}" fill="{background}"/>')
            value = "".join(char for char, _ in line[x:end])
            if value.strip():
                pieces.append(f'<text x="{PAD + x * CELL_W}" y="{PAD + y * CELL_H + 14}" '
                              f'fill="{foreground}" font-family="JetBrains Mono, Menlo, monospace" '
                              f'font-size="15" font-weight="{"700" if bold else "400"}" '
                              f'textLength="{len(value) * CELL_W}" lengthAdjust="spacing">'
                              f'{escape(value)}</text>')
            x = end
    pieces.append('</svg>')
    return "\n".join(pieces) + "\n"


if __name__ == "__main__":
    screen = Screen()
    with patch("desk.curses.color_pair", side_effect=lambda number: number << 8):
        draw_cards(screen, snapshot, rows(snapshot), 0, "", "Demo data · no live Herdr state",
                   preview=["The API response has two valid shapes.",
                            "Waiting for your choice: keep v1 or switch to v2.",
                            "Resume this agent to answer in its pane."],
                   preview_name="Review API response")
    output = Path(__file__).resolve().parents[1] / "assets" / "atlas-demo.svg"
    output.parent.mkdir(exist_ok=True)
    output.write_text(render(screen))
    print(output)
