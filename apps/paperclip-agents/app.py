#!/usr/bin/env python3
"""Paperclip Agents: your AI agent company at a glance - who's working, inbox, today's spend.

    python app.py                                   # BUSY Bar over USB (10.0.4.20)
    python app.py --host 127.0.0.1:8080             # emulator or a Wi-Fi bar
    python app.py --paperclip http://127.0.0.1:3100 # Paperclip server (default)
    python app.py --demo                            # scripted data, no Paperclip needed

Front (72x16):

    +------------------+-+-----------------+
    | o o o o o o      | | [icon]  metric  |   metric page rotates:
    | o o o o o o      | | ######::::!!!!  |   WORKING / INBOX / TODAY $
    +------------------+-+-----------------+   bottom bar = task mix

Left: one little robot per agent. Running agents are green, bob up and down
and scan their eyes; idle agents are dim and asleep; paused agents are amber;
errored agents blink red; agents awaiting hire approval are purple.

Right: a rotating metric (working agents / inbox count / today's cost) with a
pixel icon, above a stacked task bar: in progress (blue), queued (gray),
blocked (red), done (green). The inbox page jumps the queue while the inbox
is non-empty, and the envelope glows amber.

Back (160x80, if your bar has one): a text dashboard with every number.

Data comes from the Paperclip control-plane REST API:

    GET /api/companies/:id/agents          agent statuses
    GET /api/companies/:id/dashboard       task counts
    GET /api/companies/:id/sidebar-badges  approvals, failed runs, join requests
    GET /api/companies/:id/issues?touchedByUserId=me&unreadForUserId=me...
                                           unread issues in your Mine inbox
    GET /api/companies/:id/costs/summary?from=<local midnight>
                                           today's spend

A Paperclip server in local_trusted mode on the same machine needs no token.
Otherwise pass a board API key with --token or PAPERCLIP_API_KEY
(``paperclipai auth login`` creates one). The app is strictly read-only.

Run as a macOS service (per-user LaunchAgent: starts at login, restarts on
crash, not respawned after a clean stop):

    python3 app.py --install-service --host 10.0.4.20 --company <id>
    python3 app.py --service-status
    python3 app.py --uninstall-service

--install-service takes the same flags you would run the app with (minus
--once) and writes ~/Library/LaunchAgents/com.busybar.paperclip-agents.plist,
then loads it with launchctl. Re-running it replaces the service. Secrets
(--token / PAPERCLIP_API_KEY, --bar-token / BUSY_HTTP_PASSWORD) go into the
plist's EnvironmentVariables, never argv, and the plist is then chmod 0600.
The service runs with --wait-for-bar: if the bar is unreachable (asleep,
unplugged, Wi-Fi not up yet at login) it retries with backoff instead of
exiting. Logs: ~/Library/Logs/paperclip-agents.log. Inspect it with

    launchctl print gui/$(id -u)/com.busybar.paperclip-agents

Standard library only.
"""

import argparse
import json
import math
import os
import plistlib
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

APP = "paperclip-agents"
W, H = 72, 16
DRAW_PATH = "/api/display/draw"
DEVICE_TIMEOUT = 5
API_TIMEOUT = 8

FRAME_INTERVAL = 0.25    # s; ~4 fps robot animation
PAGE_SECONDS = 4.0       # s; each metric page holds this long
STALE_SECONDS = 120      # s; data older than this gets a red "stale" pixel
ELEMENT_TIMEOUT = 30     # s; dead-man window - a killed app clears itself
RETRY_MAX = 60           # s; --wait-for-bar backoff ceiling

SERVICE_LABEL = "com.busybar.paperclip-agents"
SERVICE_LOG = os.path.join("Library", "Logs", "paperclip-agents.log")

# ---- palette ---------------------------------------------------------------
WHITE = "#FFFFFFFF"
CLEAR = "#00000000"
DIM_TEXT = "#8A8A8AFF"
DIVIDER = "#333333FF"
TRACK = "#1E1E1EFF"
EYE = "#0A0A0AFF"

GREEN = "#22C55EFF"      # running
GREEN_HI = "#86EFACFF"
IDLE = "#475569FF"       # idle, asleep
AMBER = "#F59E0BFF"      # paused / inbox attention
RED = "#EF4444FF"        # error / blocked
RED_DIM = "#5A1616FF"
PURPLE = "#A855F7FF"     # pending approval
BLUE = "#3B82F6FF"       # in progress
GRAY = "#4B5563FF"       # queued
DONE = "#15803DFF"       # done
GOLD = "#FACC15FF"
GOLD_DK = "#A16207FF"

# ---- layout ----------------------------------------------------------------
SLOTS_PER_ROW = 6
ROWS = 2
MAX_SLOTS = SLOTS_PER_ROW * ROWS   # 12 robots; more agents -> "+N" badge
SLOT_PITCH = 6                     # 5 px robot + 1 px gap
ROBOT_W, ROBOT_H = 5, 6
DIVIDER_X = 37
PANEL_X = 39                       # right panel: x 39..71 (33 px)
ICON_X, ICON_Y = 40, 2
METRIC_CX = 60                     # metric text centred in x 48..71
BAR_X, BAR_Y, BAR_W, BAR_H = 39, 12, 33, 3
ICON_RECTS = 6                     # fixed icon element ids ic0..ic5

STATUS_ORDER = {"running": 0, "error": 1, "pending_approval": 2, "paused": 3, "idle": 4}


# ---- tiny element helpers ----------------------------------------------------

def rect(eid, x, y, w, h, color, radius=0, border=None, display="front"):
    """Solid rectangle. ``border`` draws an outline instead of a fill."""
    el = {
        "id": eid, "type": "rectangle", "x": int(x), "y": int(y),
        "width": max(1, int(w)), "height": max(1, int(h)), "radius": radius,
        "timeout": ELEMENT_TIMEOUT, "display": display,
    }
    if border:
        el.update(fill="none", fill_colors=[], border_width=1, border_color=border)
    else:
        el.update(fill="solid", fill_colors=[color], border_width=0, border_color=CLEAR)
    return el


def hidden(eid, display="front"):
    """Keep an id alive without drawing anything (ids must not change type)."""
    return rect(eid, 0, 0, 1, 1, CLEAR, display=display)


def text(eid, txt, x, y, font="small", color=WHITE, align="top_left", display="front"):
    return {
        "id": eid, "type": "text", "text": str(txt) if txt else " ",
        "x": int(x), "y": int(y), "font": font, "color": color, "align": align,
        "timeout": ELEMENT_TIMEOUT, "display": display,
    }


# ---- formatting --------------------------------------------------------------

def fmt_money(cents):
    """Compact dollars that fit the 24 px metric slot: 0.42 / 15.87 / 123.4 / 1.2k."""
    if cents is None:
        return "--"
    d = cents / 100.0
    if d < 100:
        return f"{d:.2f}"
    if d < 1000:
        return f"{d:.1f}"
    if d < 100000:
        return f"{d / 1000:.1f}k"
    return f"{d / 1000:.0f}k"


def fmt_money_long(cents):
    return "--" if cents is None else f"${cents / 100.0:,.2f}"


def local_midnight_utc_iso(now=None):
    """Start of the local calendar day, as an ISO UTC timestamp for ``from=``."""
    local = datetime.fromtimestamp(now or time.time()).astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- Paperclip client ----------------------------------------------------------

class PaperclipError(Exception):
    pass


class Paperclip:
    """Read-only client for the handful of endpoints this app needs."""

    def __init__(self, base, token, company):
        self.base = base.rstrip("/")
        self.token = token
        self.company = company

    def get(self, path, **params):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise PaperclipError(f"HTTP {exc.code} on {path}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise PaperclipError(f"{path}: {exc}") from exc

    def resolve_company(self):
        if self.company:
            return self.company
        companies = self.get("/api/companies")
        active = [c for c in companies if c.get("status", "active") == "active"] or companies
        if not active:
            raise PaperclipError("no companies visible to this token")
        self.company = active[0]["id"]
        return self.company

    def snapshot(self, want_cost=True, previous=None):
        """One poll -> a plain dict the renderer understands."""
        cid = self.resolve_company()
        c = f"/api/companies/{cid}"
        agents = [a for a in self.get(c + "/agents")
                  if a.get("status") not in ("terminated",)]
        dash = self.get(c + "/dashboard")
        badges = self.get(c + "/sidebar-badges")

        # Unread issues in the Mine inbox need board (user) auth. With an agent
        # token the filter is rejected; fall back to the badge counts alone.
        unread = 0
        try:
            mine = self.get(c + "/issues", touchedByUserId="me",
                            inboxArchivedByUserId="me", unreadForUserId="me",
                            status="backlog,todo,in_progress,in_review,blocked,done",
                            limit=200)
            unread = len(mine) if isinstance(mine, list) else 0
        except PaperclipError:
            unread = 0

        cost = previous.get("today_cents") if previous else None
        if want_cost or cost is None:
            summary = self.get(c + "/costs/summary", **{"from": local_midnight_utc_iso()})
            cost = float(summary.get("spendCents") or 0)

        tasks = dash.get("tasks", {})
        return {
            "agents": [{"name": a.get("name", "?"), "status": a.get("status", "idle")}
                       for a in agents],
            "inbox": int(badges.get("inbox", 0)) + unread,
            "approvals": int(badges.get("approvals", 0)),
            "failed_runs": int(badges.get("failedRuns", 0)),
            "today_cents": cost,
            "month_cents": float((dash.get("costs") or {}).get("monthSpendCents") or 0),
            "tasks": {
                "open": int(tasks.get("open", 0)),
                "in_progress": int(tasks.get("inProgress", 0)),
                "blocked": int(tasks.get("blocked", 0)),
                "done": int(tasks.get("done", 0)),
            },
            "fetched_at": time.time(),
        }


# ---- demo data -----------------------------------------------------------------

DEMO_NAMES = ["Friday", "Kimchi", "Ponch", "Harlan", "Jamey", "Kybana",
              "Docker", "Ansible", "Frontend", "Backend"]


def demo_snapshot(t):
    """A scripted company that drifts through busy and quiet moments."""
    phase = int(t // 6) % 4
    running = [3, 5, 2, 6][phase]
    agents = []
    for i, name in enumerate(DEMO_NAMES):
        status = "running" if i < running else "idle"
        if i == 8:
            status = "error" if phase in (1, 2) else status
        if i == 9:
            status = "paused"
        agents.append({"name": name, "status": status})
    inbox = [2, 3, 0, 1][phase]
    return {
        "agents": agents,
        "inbox": inbox,
        "approvals": 1 if inbox else 0,
        "failed_runs": 1 if phase in (1, 2) else 0,
        "today_cents": 1587.28 + t * 3.1,
        "month_cents": 7195.77 + t * 3.1,
        "tasks": {"open": 14, "in_progress": running, "blocked": 4, "done": 20},
        "fetched_at": time.time(),
    }


# ---- rendering -------------------------------------------------------------------

def robot_elements(slot, agent, tick):
    """Three elements per robot slot: antenna ``ra<n>``, body ``rb<n>``, eyes ``re<n>``."""
    col, row = slot % SLOTS_PER_ROW, slot // SLOTS_PER_ROW
    x0 = col * SLOT_PITCH
    y0 = row * 8 + 1                 # antenna row; body starts one below
    ids = (f"ra{slot}", f"rb{slot}", f"re{slot}")
    if agent is None:
        return [hidden(i) for i in ids]

    status = agent["status"]
    phase = tick + slot * 3          # de-sync the robots
    if status == "running":
        bob = (phase // 2) % 2       # 1 px hop every half second
        look = (phase // 3) % 2      # eyes glance left/right
        tip = GREEN_HI if phase % 4 < 2 else WHITE
        y = y0 - bob
        return [rect(ids[0], x0 + 2, y, 1, 1, tip),
                rect(ids[1], x0, y + 1, ROBOT_W, ROBOT_H - 1, GREEN),
                rect(ids[2], x0 + 1 + look, y + 3, 2, 1, EYE)]
    if status == "error":
        on = (phase // 2) % 2 == 0
        return [rect(ids[0], x0 + 2, y0, 1, 1, WHITE if on else RED),
                rect(ids[1], x0, y0 + 1, ROBOT_W, ROBOT_H - 1, RED if on else RED_DIM),
                rect(ids[2], x0 + 1, y0 + 3, 3, 1, EYE)]
    color = {"paused": AMBER, "pending_approval": PURPLE}.get(status, IDLE)
    # idle / paused / pending: slumped one pixel lower, antenna dark, eyes shut
    return [hidden(ids[0]),
            rect(ids[1], x0, y0 + 2, ROBOT_W, ROBOT_H - 2, color),
            rect(ids[2], x0 + 1, y0 + 4, 3, 1, EYE)]


def agent_area(agents, tick):
    ordered = sorted(agents, key=lambda a: STATUS_ORDER.get(a["status"], 5))
    overflow = 0
    if len(ordered) > MAX_SLOTS:
        overflow = len(ordered) - (MAX_SLOTS - 1)
        ordered = ordered[:MAX_SLOTS - 1]
    els = []
    for slot in range(MAX_SLOTS):
        els += robot_elements(slot, ordered[slot] if slot < len(ordered) else None, tick)
    els.append(text("more", f"+{overflow}" if overflow else " ", 35, 15,
                    font="tiny", color=DIM_TEXT, align="bottom_right"))
    return els


def icon_rects(kind, data, tick):
    """Up to ICON_RECTS rectangles forming a 7x7-ish pixel icon at ICON_X/Y."""
    x, y = ICON_X, ICON_Y
    rs = []
    if kind == "working":
        # a bolt-ish play triangle
        c = GREEN if data else GRAY
        rs = [(x + 1, y + 1, 1, 7, c), (x + 2, y + 2, 1, 5, c),
              (x + 3, y + 3, 1, 3, c), (x + 4, y + 4, 1, 1, c)]
    elif kind == "inbox":
        attn = data > 0
        c = AMBER if attn and (tick // 2) % 2 == 0 else (AMBER if attn else GRAY)
        if attn and (tick // 2) % 4 == 3:
            c = "#FDE68AFF"
        rs = [("outline", x, y + 1, 7, 6, c),
              (x + 1, y + 2, 1, 1, c), (x + 5, y + 2, 1, 1, c),
              (x + 2, y + 3, 1, 1, c), (x + 4, y + 3, 1, 1, c),
              (x + 3, y + 4, 1, 1, c)]
    elif kind == "cost":
        shine = (tick // 2) % 6 == 0
        rs = [(x + 1, y + 1, 5, 7, GOLD), (x, y + 2, 7, 5, GOLD),
              (x + 3, y + 2, 1, 5, GOLD_DK), (x + 2, y + 3, 1, 1, GOLD_DK),
              (x + 4, y + 5, 1, 1, GOLD_DK)]
        if shine:
            rs.append((x + 1, y + 2, 1, 1, WHITE))
    els = []
    for i in range(ICON_RECTS):
        eid = f"ic{i}"
        if i >= len(rs):
            els.append(hidden(eid))
            continue
        r = rs[i]
        if r[0] == "outline":
            _, rx, ry, rw, rh, rc = r
            els.append(rect(eid, rx, ry, rw, rh, None, border=rc))
        elif len(r) == 6:
            rx, ry, rw, rh, rc, rad = r
            els.append(rect(eid, rx, ry, rw, rh, rc, radius=rad))
        else:
            rx, ry, rw, rh, rc = r
            els.append(rect(eid, rx, ry, rw, rh, rc))
    return els[:ICON_RECTS]


def page_for(now, snap):
    """Rotate WORKING -> INBOX -> COST; a non-empty inbox gets every other slot."""
    pages = ["working", "inbox", "cost"]
    if snap and snap["inbox"] > 0:
        pages = ["inbox", "working", "inbox", "cost"]
    return pages[int(now // PAGE_SECONDS) % len(pages)]


def metric_elements(page, snap, tick):
    if snap is None:
        return icon_rects(None, 0, tick) + [text("metric", "...", METRIC_CX, 1,
                                                  font="condensed", color=DIM_TEXT,
                                                  align="top_mid")]
    agents = snap["agents"]
    if page == "working":
        n = sum(1 for a in agents if a["status"] == "running")
        label, color = f"{n}/{len(agents)}", (GREEN if n else DIM_TEXT)
        data = n
    elif page == "inbox":
        n = snap["inbox"]
        label, color = str(n), (AMBER if n else DIM_TEXT)
        data = n
    else:
        label, color = fmt_money(snap["today_cents"]), WHITE
        data = snap["today_cents"]
    return icon_rects(page, data, tick) + [
        text("metric", label, METRIC_CX, 1, font="condensed", color=color, align="top_mid")]


def task_bar(snap):
    """Stacked bar: in progress / queued / blocked / done, proportional widths."""
    ids = ["tb_ip", "tb_q", "tb_bl", "tb_dn"]
    els = [rect("tb_track", BAR_X, BAR_Y, BAR_W, BAR_H, TRACK)]
    if not snap:
        return els + [hidden(i) for i in ids]
    t = snap["tasks"]
    queued = max(0, t["open"] - t["in_progress"] - t["blocked"])
    parts = [(t["in_progress"], BLUE), (queued, GRAY), (t["blocked"], RED), (t["done"], DONE)]
    total = sum(p for p, _ in parts)
    if total <= 0:
        return els + [hidden(i) for i in ids]
    # largest-remainder rounding so the segments always sum to BAR_W
    raw = [p * BAR_W / total for p, _ in parts]
    widths = [int(r) for r in raw]
    for i in sorted(range(4), key=lambda i: raw[i] - widths[i], reverse=True)[:BAR_W - sum(widths)]:
        widths[i] += 1
    x = BAR_X
    for eid, w, (count, color) in zip(ids, widths, parts):
        if w <= 0:
            els.append(hidden(eid))
            continue
        els.append(rect(eid, x, BAR_Y, w, BAR_H, color))
        x += w
    return els


def back_elements(snap, stale):
    b = "back"
    if snap is None:
        lines = ["CONNECTING...", " ", " ", " ", " "]
    else:
        a = snap["agents"]
        running = sum(1 for x in a if x["status"] == "running")
        errors = sum(1 for x in a if x["status"] == "error")
        t = snap["tasks"]
        lines = [
            f"WORKING  {running}/{len(a)}" + (f"  ERR {errors}" if errors else ""),
            f"INBOX    {snap['inbox']}",
            f"TODAY    {fmt_money_long(snap['today_cents'])}",
            f"MONTH    {fmt_money_long(snap['month_cents'])}",
            f"TASKS {t['in_progress']} run {t['blocked']} blk {t['done']} done",
        ]
        if stale:
            lines[4] = "STALE - PAPERCLIP OFFLINE?"
    els = [text("b_title", "PAPERCLIP", 80, 1, font="normal", color=WHITE,
                align="top_mid", display=b)]
    for i, line in enumerate(lines):
        els.append(text(f"b_l{i}", line, 2, 14 + i * 13, font="normal",
                        color=WHITE if i < 4 else DIM_TEXT, display=b))
    return els


def build_frame(snap, now, tick, stale):
    els = agent_area(snap["agents"] if snap else [], tick)
    els.append(rect("divider", DIVIDER_X, 0, 1, H, RED if stale else DIVIDER))
    els += metric_elements(page_for(now, snap), snap, tick)
    els += task_bar(snap)
    els += back_elements(snap, stale)
    return els


# ---- BUSY Bar HTTP API (stdlib only; docs: http://10.0.4.20/docs) ----------------

class DeviceError(Exception):
    pass


class BusyBar:
    def __init__(self, host, token, priority):
        self.base = "http://" + host.strip().replace("http://", "", 1).rstrip("/")
        self.token = token
        self.priority = priority

    def _send(self, req):
        if self.token:
            req.add_header("X-API-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=DEVICE_TIMEOUT) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as exc:
            raise DeviceError(str(exc)) from exc

    def draw(self, elements):
        body = json.dumps({"application_name": APP, "priority": self.priority,
                           "elements": elements}).encode()
        req = urllib.request.Request(self.base + DRAW_PATH, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        return self._send(req)

    def clear(self):
        req = urllib.request.Request(f"{self.base}{DRAW_PATH}?application_name={APP}",
                                     method="DELETE")
        return self._send(req)


# ---- main --------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="10.0.4.20",
                   help="BUSY Bar ip[:port] (default 10.0.4.20; emulator: 127.0.0.1:8080)")
    p.add_argument("--bar-token", default=os.environ.get("BUSY_HTTP_PASSWORD"),
                   help="BUSY Bar HTTP API password (or BUSY_HTTP_PASSWORD)")
    p.add_argument("--paperclip", default=os.environ.get("PAPERCLIP_API_URL", "http://127.0.0.1:3100"),
                   help="Paperclip server URL (default http://127.0.0.1:3100)")
    p.add_argument("--token", default=os.environ.get("PAPERCLIP_API_KEY"),
                   help="Paperclip board API key (or PAPERCLIP_API_KEY); "
                        "not needed for a local_trusted server on this machine")
    p.add_argument("--company", default=os.environ.get("PAPERCLIP_COMPANY_ID"),
                   help="company id (default: first active company)")
    p.add_argument("--poll", type=float, default=15.0,
                   help="seconds between status polls (default 15)")
    p.add_argument("--cost-poll", type=float, default=60.0,
                   help="seconds between cost refreshes (default 60)")
    p.add_argument("--priority", type=int, default=20,
                   help="draw priority 1-100 (default 20, yields to notifications)")
    p.add_argument("--demo", action="store_true", help="scripted data, no Paperclip needed")
    p.add_argument("--once", "--test", action="store_true", dest="once",
                   help="draw one frame and exit")
    p.add_argument("--wait-for-bar", action="store_true",
                   help="if the bar is unreachable, retry with backoff instead of "
                        "exiting (the macOS service always uses this)")
    svc = p.add_mutually_exclusive_group()
    svc.add_argument("--install-service", action="store_true",
                     help="macOS: install and start a LaunchAgent running this app "
                          "with the other flags given")
    svc.add_argument("--uninstall-service", action="store_true",
                     help="macOS: stop and remove the LaunchAgent")
    svc.add_argument("--service-status", action="store_true",
                     help="macOS: show the LaunchAgent's launchctl status")
    args = p.parse_args(argv)
    if not 1 <= args.priority <= 100:
        p.error("--priority must be between 1 and 100")
    return args


# ---- macOS launchd service -----------------------------------------------------------

def service_plist_path(home=None):
    return os.path.join(home or os.path.expanduser("~"), "Library", "LaunchAgents",
                        SERVICE_LABEL + ".plist")


def build_service_plist(args, app_path=None, python=None, home=None):
    """The LaunchAgent as a dict (pure: no filesystem or launchctl access).

    Uses the *resolved* option values, so settings that came from environment
    variables in this shell (PAPERCLIP_API_URL, PAPERCLIP_COMPANY_ID, ...) are
    pinned into the service, which won't see this shell's environment.
    """
    home = home or os.path.expanduser("~")
    prog = [python or sys.executable, os.path.abspath(app_path or __file__),
            "--host", args.host,
            "--poll", f"{args.poll:g}",
            "--cost-poll", f"{args.cost_poll:g}",
            "--priority", str(args.priority),
            "--wait-for-bar"]
    if args.demo:
        prog.append("--demo")
    else:
        prog += ["--paperclip", args.paperclip]
        if args.company:
            prog += ["--company", args.company]
    # --once is never persisted: a one-shot exits 0, which KeepAlive won't respawn.

    env = {"PYTHONUNBUFFERED": "1"}   # log lines land in the log file promptly
    if args.token and not args.demo:
        env["PAPERCLIP_API_KEY"] = args.token
    if args.bar_token:
        env["BUSY_HTTP_PASSWORD"] = args.bar_token

    log = os.path.join(home, SERVICE_LOG)
    return {
        "Label": SERVICE_LABEL,
        "ProgramArguments": prog,
        "EnvironmentVariables": env,
        "WorkingDirectory": os.path.dirname(prog[1]),
        "RunAtLoad": True,
        # Respawn after a crash or error exit, but not after a clean stop.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        # Interactive: a 4 fps display loop; Background would get its timers
        # coalesced and CPU throttled, making the robots stutter.
        "ProcessType": "Interactive",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def plist_has_secrets(plist):
    env = plist.get("EnvironmentVariables", {})
    return any(k in env for k in ("PAPERCLIP_API_KEY", "BUSY_HTTP_PASSWORD"))


def _launchctl(*cmd):
    return subprocess.run(["launchctl", *cmd], capture_output=True, text=True)


def _require_macos():
    if sys.platform != "darwin":
        sys.exit("error: --install-service/--uninstall-service/--service-status use "
                 f"launchd and only work on macOS (this is {sys.platform}); "
                 "on Linux run the app under systemd or your init of choice")


def install_service(args):
    _require_macos()
    if args.once:
        print("note: --once is not persisted into the service", file=sys.stderr)
    plist = build_service_plist(args)
    path = service_plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(os.path.dirname(plist["StandardOutPath"]), exist_ok=True)

    uid = os.getuid()
    domain, target = f"gui/{uid}", f"gui/{uid}/{SERVICE_LABEL}"
    _launchctl("bootout", target)          # idempotent reinstall; fails if not loaded

    # Write with 0600 from the start when it holds secrets, so no window
    # where the token is world-readable.
    mode = 0o600 if plist_has_secrets(plist) else 0o644
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as fh:
        plistlib.dump(plist, fh)
    os.chmod(path, mode)                   # O_CREAT mode is ignored for an existing file

    _launchctl("enable", target)           # clear a disable left by `unload -w`
    for attempt in range(5):
        res = _launchctl("bootstrap", domain, path)
        if res.returncode == 0:
            break
        # bootout is asynchronous; bootstrap can race it ("Input/output error").
        time.sleep(0.5 * (attempt + 1))
    else:
        legacy = _launchctl("load", "-w", path)   # pre-10.10 macOS
        if legacy.returncode != 0:
            sys.exit(f"error: launchctl could not load {path}:\n"
                     f"{(res.stderr or res.stdout).strip()}\n{legacy.stderr.strip()}")
    print(f"installed {SERVICE_LABEL}\n  plist: {path}"
          f"{' (0600, contains secrets)' if mode == 0o600 else ''}\n"
          f"  log:   {plist['StandardOutPath']}\n"
          f"  run:   {' '.join(plist['ProgramArguments'])}\n"
          f"  check: launchctl print {target}")
    return 0


def uninstall_service():
    _require_macos()
    path = service_plist_path()
    res = _launchctl("bootout", f"gui/{os.getuid()}/{SERVICE_LABEL}")
    if res.returncode != 0 and os.path.exists(path):
        _launchctl("unload", "-w", path)   # pre-10.10 macOS
    if os.path.exists(path):
        os.remove(path)
        print(f"uninstalled {SERVICE_LABEL} (removed {path})")
    else:
        print(f"{SERVICE_LABEL} was not installed")
    return 0


def service_status():
    _require_macos()
    path = service_plist_path()
    print(f"plist: {path} ({'present' if os.path.exists(path) else 'missing'})")
    res = _launchctl("print", f"gui/{os.getuid()}/{SERVICE_LABEL}")
    if res.returncode != 0:
        print("not loaded")
        return 1
    for line in res.stdout.splitlines():
        s = line.strip()
        if s.startswith(("state =", "pid =", "last exit code", "runs =")):
            print(s)
    return 0


def _sigterm(signum, frame):
    # launchd (on stop/bootout), systemd and a bare `kill` all send SIGTERM.
    # Raising here puts them on the Ctrl-C path: one exit that clears the display.
    raise KeyboardInterrupt


def main(argv=None):
    args = parse_args(argv)
    if args.install_service:
        return install_service(args)
    if args.uninstall_service:
        return uninstall_service()
    if args.service_status:
        return service_status()

    signal.signal(signal.SIGTERM, _sigterm)
    bar = BusyBar(args.host, args.bar_token, args.priority)
    pc = Paperclip(args.paperclip, args.token, args.company)

    snap = None
    last_poll = last_cost = 0.0
    last_error = None
    blocked = False
    bar_down = 0             # consecutive unreachable draws (--wait-for-bar)
    start = time.time()
    tick = 0

    print(f"{APP} -> {bar.base}  (Paperclip: {'demo' if args.demo else args.paperclip})"
          "  Ctrl-C to stop", flush=True)
    try:
        while True:
            now = time.time()
            if args.demo:
                snap = demo_snapshot(now - start)
            elif last_poll == 0.0 or now - last_poll >= args.poll:
                last_poll = now
                want_cost = last_cost == 0.0 or now - last_cost >= args.cost_poll
                try:
                    snap = pc.snapshot(want_cost=want_cost, previous=snap)
                    if want_cost:
                        last_cost = now
                    if last_error:
                        print("paperclip reachable again", flush=True)
                    last_error = None
                except PaperclipError as exc:
                    if str(exc) != last_error:
                        print(f"paperclip unavailable: {exc}", file=sys.stderr, flush=True)
                    last_error = str(exc)

            stale = snap is not None and now - snap["fetched_at"] > STALE_SECONDS
            try:
                status, body = bar.draw(build_frame(snap, now, tick, stale))
            except DeviceError as exc:
                if args.once or not args.wait_for_bar:
                    raise
                # Back off 2, 4, 8 ... RETRY_MAX s; log only the first failure.
                bar_down += 1
                delay = min(RETRY_MAX, 2 ** bar_down)
                if bar_down == 1:
                    print(f"bar unreachable ({exc}); retrying with backoff...",
                          file=sys.stderr, flush=True)
                time.sleep(delay)
                continue
            if bar_down:
                print("bar reachable again", flush=True)
                bar_down = 0
            if status == 200:
                blocked = False
            elif status == 409:
                if args.once:
                    sys.exit("error: display busy, another app has priority")
                if not blocked:
                    print("display busy: another app has priority, waiting...", flush=True)
                    blocked = True
            elif status in (401, 403):
                sys.exit("error: bar rejected the token; set BUSY_HTTP_PASSWORD or --bar-token")
            else:
                sys.exit(f"error: bar returned HTTP {status}: {body.strip()}")

            if args.once:
                return 0
            tick += 1
            time.sleep(FRAME_INTERVAL)
    except KeyboardInterrupt:
        try:
            bar.clear()
        except DeviceError:
            pass
        print("\nstopped.")
        return 0
    except DeviceError as exc:
        sys.exit(f"error: {bar.base} unreachable: {exc}")


if __name__ == "__main__":
    sys.exit(main())
