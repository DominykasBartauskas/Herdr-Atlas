# Herdr Atlas

A full-screen desk for live Herdr workspaces, worktrees, and agents. It uses Herdr's CLI and local event socket with only Python's standard library. The desk refreshes on workspace, worktree, pane, and agent-state events, with a five-second fallback poll.

Clone and link the plugin, then open the desk in a new Herdr tab:

```sh
git clone https://github.com/DominykasBartauskas/Herdr-Atlas.git
cd Herdr-Atlas
herdr plugin link .
herdr plugin pane open --plugin sacrive.herdr-atlas --entrypoint desk
```

Click a workspace card to select it; double-click to open it. The attention strip jumps directly to blocked or finished agents. Click the filter labels for **All**, **Active**, or **No agent**, and use **Pin** or `p` to keep a workspace at the front. Pins persist in Herdr's plugin state directory. Workspaces sort by repository; `g` folds or expands the selected repository. `/` searches across workspaces, checkout paths, agents, and statuses.

Click **+ New** or press `n` on a repository workspace to create a worktree and its Herdr workspace. The form asks for a branch, optional base ref, and label. The detail panel shows the selected agent's latest output; `a` cycles previewed agents, and **Resume agent** or `v` focuses the previewed agent. Clicking a listed agent or pressing `1`–`4` also jumps to it.

**Cleanup** or `c` shows open linked worktrees with no live agent and closed linked checkouts found through `herdr worktree list`. Open worktrees can be focused, closed with `d`, or removed with `x`. Closing keeps the checkout; removing asks you to type `REMOVE`, then uses Herdr's non-forced removal command, which rejects dirty checkouts. Closed checkouts can be reopened from the desk. Mouse clicks work on cards and visible actions; the wheel scrolls the board. Arrow keys or `h`/`j`/`k`/`l` navigate, Enter opens, `f` cycles filters, `r` refreshes, and `q` quits.

Python 3 must be available on Herdr's PATH. Linking a local plugin does not run a build step. Historical sessions that Herdr has already closed are outside the live agent preview.
