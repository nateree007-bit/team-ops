import base64
import os
import secrets
from datetime import date, datetime, timedelta

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from . import db

AUTH_USER = os.environ.get("TEAM_OPS_USER")
AUTH_PASSWORD = os.environ.get("TEAM_OPS_PASSWORD")


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Gates every request behind HTTP Basic Auth, but only when
    TEAM_OPS_USER/TEAM_OPS_PASSWORD are set (production). Local runs
    without those env vars stay open, so `run.bat` needs no setup."""

    async def dispatch(self, request: Request, call_next):
        if not AUTH_USER or not AUTH_PASSWORD:
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        scheme, _, credentials = auth_header.partition(" ")
        if scheme.lower() == "basic":
            try:
                decoded = base64.b64decode(credentials).decode()
                user, _, password = decoded.partition(":")
            except Exception:
                user, password = "", ""
            if secrets.compare_digest(user, AUTH_USER) and secrets.compare_digest(
                password, AUTH_PASSWORD
            ):
                return await call_next(request)

        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Team Ops"'},
        )


app = FastAPI(title="Team Ops")
app.add_middleware(BasicAuthMiddleware)

templates = Jinja2Templates(directory=str(__import__("pathlib").Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(__import__("pathlib").Path(__file__).parent / "static")), name="static")

db.init_db()


# ---------- Dashboard ----------

@app.get("/")
def dashboard(request: Request):
    conn = db.get_connection()
    today = date.today().isoformat()
    upcoming = conn.execute(
        "SELECT * FROM events WHERE event_date >= ? ORDER BY event_date, event_time LIMIT 5",
        (today,),
    ).fetchall()
    open_tasks = conn.execute(
        "SELECT * FROM tasks WHERE done = 0 ORDER BY (due_date IS NULL), due_date LIMIT 8"
    ).fetchall()
    injured = conn.execute(
        "SELECT * FROM players WHERE status != 'active' ORDER BY name"
    ).fetchall()
    counts = {
        "players": conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"],
        "upcoming_events": conn.execute(
            "SELECT COUNT(*) c FROM events WHERE event_date >= ?", (today,)
        ).fetchone()["c"],
        "open_tasks": conn.execute("SELECT COUNT(*) c FROM tasks WHERE done = 0").fetchone()["c"],
    }
    conn.close()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "upcoming": upcoming,
            "open_tasks": open_tasks,
            "injured": injured,
            "counts": counts,
            "active": "dashboard",
        },
    )


# ---------- Players / Roster ----------

@app.get("/players")
def list_players(request: Request):
    conn = db.get_connection()
    players = conn.execute("SELECT * FROM players ORDER BY name").fetchall()
    conn.close()
    return templates.TemplateResponse(
        request, "players.html", {"players": players, "active": "players"}
    )


@app.post("/players/new")
def create_player(
    name: str = Form(...),
    jersey_number: str = Form(""),
    position: str = Form(""),
    status: str = Form("active"),
    notes: str = Form(""),
):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO players (name, jersey_number, position, status, notes) VALUES (?, ?, ?, ?, ?)",
        (name, jersey_number, position, status, notes),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/players", status_code=303)


@app.get("/players/{player_id}")
def player_detail(request: Request, player_id: int):
    conn = db.get_connection()
    player = conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    stats = conn.execute(
        """SELECT game_stats.*, events.title, events.event_date, events.opponent
           FROM game_stats JOIN events ON events.id = game_stats.event_id
           WHERE player_id = ? ORDER BY events.event_date DESC""",
        (player_id,),
    ).fetchall()
    conn.close()
    return templates.TemplateResponse(
        request,
        "player_detail.html",
        {"player": player, "stats": stats, "active": "players"},
    )


@app.post("/players/{player_id}/edit")
def edit_player(
    player_id: int,
    name: str = Form(...),
    jersey_number: str = Form(""),
    position: str = Form(""),
    status: str = Form("active"),
    notes: str = Form(""),
):
    conn = db.get_connection()
    conn.execute(
        "UPDATE players SET name=?, jersey_number=?, position=?, status=?, notes=? WHERE id=?",
        (name, jersey_number, position, status, notes, player_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/players/{player_id}", status_code=303)


@app.post("/players/{player_id}/delete")
def delete_player(player_id: int):
    conn = db.get_connection()
    conn.execute("DELETE FROM players WHERE id = ?", (player_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/players", status_code=303)


# ---------- Schedule / Events ----------

@app.get("/schedule")
def list_schedule(request: Request):
    conn = db.get_connection()
    events = conn.execute("SELECT * FROM events ORDER BY event_date, event_time").fetchall()
    conn.close()
    return templates.TemplateResponse(
        request, "schedule.html", {"events": events, "active": "schedule"}
    )


@app.post("/schedule/new")
def create_event(
    type: str = Form(...),
    title: str = Form(...),
    event_date: str = Form(...),
    event_time: str = Form(""),
    location: str = Form(""),
    opponent: str = Form(""),
    notes: str = Form(""),
):
    conn = db.get_connection()
    conn.execute(
        """INSERT INTO events (type, title, event_date, event_time, location, opponent, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (type, title, event_date, event_time, location, opponent, notes),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/schedule", status_code=303)


@app.get("/schedule/{event_id}")
def event_detail(request: Request, event_id: int):
    conn = db.get_connection()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    players = conn.execute("SELECT * FROM players ORDER BY name").fetchall()
    existing_stats = {
        row["player_id"]: row
        for row in conn.execute(
            "SELECT * FROM game_stats WHERE event_id = ?", (event_id,)
        ).fetchall()
    }
    report = conn.execute(
        "SELECT * FROM scouting_reports WHERE event_id = ?", (event_id,)
    ).fetchone()
    conn.close()
    return templates.TemplateResponse(
        request,
        "event_detail.html",
        {
            "event": event,
            "players": players,
            "existing_stats": existing_stats,
            "report": report,
            "active": "schedule",
        },
    )


@app.post("/schedule/{event_id}/edit")
def edit_event(
    event_id: int,
    type: str = Form(...),
    title: str = Form(...),
    event_date: str = Form(...),
    event_time: str = Form(""),
    location: str = Form(""),
    opponent: str = Form(""),
    notes: str = Form(""),
):
    conn = db.get_connection()
    conn.execute(
        """UPDATE events SET type=?, title=?, event_date=?, event_time=?, location=?, opponent=?, notes=?
           WHERE id=?""",
        (type, title, event_date, event_time, location, opponent, notes, event_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/schedule/{event_id}", status_code=303)


@app.post("/schedule/{event_id}/delete")
def delete_event(event_id: int):
    conn = db.get_connection()
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/schedule", status_code=303)


@app.post("/schedule/{event_id}/stats")
async def save_stats(event_id: int, request: Request):
    form = await request.form()
    player_ids = form.getlist("player_id")
    conn = db.get_connection()
    for pid in player_ids:
        minutes = form.get(f"minutes_{pid}") or 0
        points = form.get(f"points_{pid}") or 0
        rebounds = form.get(f"rebounds_{pid}") or 0
        assists = form.get(f"assists_{pid}") or 0
        steals = form.get(f"steals_{pid}") or 0
        blocks = form.get(f"blocks_{pid}") or 0
        turnovers = form.get(f"turnovers_{pid}") or 0
        fouls = form.get(f"fouls_{pid}") or 0
        notes = form.get(f"notes_{pid}") or ""
        conn.execute(
            """INSERT INTO game_stats
               (event_id, player_id, minutes, points, rebounds, assists, steals, blocks, turnovers, fouls, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(event_id, player_id) DO UPDATE SET
                 minutes=excluded.minutes, points=excluded.points, rebounds=excluded.rebounds,
                 assists=excluded.assists, steals=excluded.steals, blocks=excluded.blocks,
                 turnovers=excluded.turnovers, fouls=excluded.fouls, notes=excluded.notes""",
            (event_id, pid, minutes, points, rebounds, assists, steals, blocks, turnovers, fouls, notes),
        )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/schedule/{event_id}", status_code=303)


# ---------- Scouting Reports ----------

@app.get("/scouting")
def list_scouting(request: Request):
    conn = db.get_connection()
    reports = conn.execute(
        "SELECT * FROM scouting_reports ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse(
        request, "scouting.html", {"reports": reports, "active": "scouting"}
    )


@app.post("/scouting/new")
def create_report(
    opponent: str = Form(...),
    event_id: str = Form(""),
    content: str = Form(""),
):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO scouting_reports (opponent, event_id, content) VALUES (?, ?, ?)",
        (opponent, event_id or None, content),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/scouting", status_code=303)


@app.get("/scouting/{report_id}")
def scouting_detail(request: Request, report_id: int):
    conn = db.get_connection()
    report = conn.execute("SELECT * FROM scouting_reports WHERE id = ?", (report_id,)).fetchone()
    events = conn.execute("SELECT * FROM events ORDER BY event_date DESC").fetchall()
    conn.close()
    return templates.TemplateResponse(
        request,
        "scouting_detail.html",
        {"report": report, "events": events, "active": "scouting"},
    )


@app.post("/scouting/{report_id}/edit")
def edit_report(
    report_id: int,
    opponent: str = Form(...),
    event_id: str = Form(""),
    content: str = Form(""),
):
    conn = db.get_connection()
    conn.execute(
        """UPDATE scouting_reports SET opponent=?, event_id=?, content=?, updated_at=?
           WHERE id=?""",
        (opponent, event_id or None, content, datetime.now().isoformat(timespec="seconds"), report_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/scouting/{report_id}", status_code=303)


@app.post("/scouting/{report_id}/delete")
def delete_report(report_id: int):
    conn = db.get_connection()
    conn.execute("DELETE FROM scouting_reports WHERE id = ?", (report_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/scouting", status_code=303)


# ---------- Tasks / Misc ----------

@app.get("/tasks")
def list_tasks(request: Request):
    conn = db.get_connection()
    tasks = conn.execute(
        "SELECT * FROM tasks ORDER BY done, (due_date IS NULL), due_date"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse(
        request, "tasks.html", {"tasks": tasks, "active": "tasks"}
    )


@app.post("/tasks/new")
def create_task(
    title: str = Form(...),
    description: str = Form(""),
    due_date: str = Form(""),
):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO tasks (title, description, due_date) VALUES (?, ?, ?)",
        (title, description, due_date or None),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/tasks", status_code=303)


@app.post("/tasks/{task_id}/toggle")
def toggle_task(task_id: int):
    conn = db.get_connection()
    conn.execute("UPDATE tasks SET done = 1 - done WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/tasks", status_code=303)


@app.post("/tasks/{task_id}/delete")
def delete_task(task_id: int):
    conn = db.get_connection()
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/tasks", status_code=303)


# ---------- 300 Club ----------

GOAL = 300


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


@app.get("/threehundred")
def three_hundred(request: Request, week: str = ""):
    today = date.today()
    if week:
        try:
            requested = datetime.strptime(week, "%Y-%m-%d").date()
        except ValueError:
            requested = today
    else:
        requested = today
    wk_start = _week_start(requested)
    week_dates = [wk_start + timedelta(days=i) for i in range(7)]
    wk_end = week_dates[-1]
    day_headers = [f"{d.strftime('%a')} {d.month}/{d.day}" for d in week_dates]
    week_label = f"{wk_start.strftime('%b')} {wk_start.day} – {wk_end.strftime('%b')} {wk_end.day}, {wk_end.year}"

    conn = db.get_connection()
    players = conn.execute("SELECT * FROM players ORDER BY name").fetchall()

    logs = conn.execute(
        "SELECT * FROM shot_logs WHERE log_date BETWEEN ? AND ?",
        (week_dates[0].isoformat(), wk_end.isoformat()),
    ).fetchall()
    log_map = {(row["player_id"], row["log_date"]): row["makes"] for row in logs}

    grid = []
    week_totals = []
    for p in players:
        day_values = [log_map.get((p["id"], d.isoformat())) for d in week_dates]
        total = sum(v for v in day_values if v is not None)
        grid.append({"player": p, "days": day_values, "total": total})
        week_totals.append({"player": p, "total": total})
    week_totals.sort(key=lambda x: -x["total"])

    all_time_rows = conn.execute(
        "SELECT player_id, SUM(makes) as total FROM shot_logs GROUP BY player_id"
    ).fetchall()
    all_time_map = {r["player_id"]: (r["total"] or 0) for r in all_time_rows}
    all_time = sorted(
        ({"player": p, "total": all_time_map.get(p["id"], 0)} for p in players),
        key=lambda x: -x["total"],
    )

    all_logs = conn.execute(
        "SELECT player_id, log_date, makes FROM shot_logs ORDER BY player_id, log_date"
    ).fetchall()
    by_player: dict[int, list] = {}
    for r in all_logs:
        by_player.setdefault(r["player_id"], []).append((r["log_date"], r["makes"]))

    streaks = []
    for p in players:
        entries = by_player.get(p["id"], [])

        longest = 0
        run = 0
        prev_date = None
        for log_date_str, makes in entries:
            d = datetime.strptime(log_date_str, "%Y-%m-%d").date()
            hit = (makes or 0) >= GOAL
            if hit:
                run = run + 1 if prev_date is not None and (d - prev_date).days == 1 and run > 0 else 1
            else:
                run = 0
            longest = max(longest, run)
            prev_date = d

        current = 0
        prev_date = None
        for log_date_str, makes in reversed(entries):
            d = datetime.strptime(log_date_str, "%Y-%m-%d").date()
            if (makes or 0) < GOAL:
                break
            if prev_date is None or (prev_date - d).days == 1:
                current += 1
                prev_date = d
            else:
                break

        streaks.append({"player": p, "longest": longest, "current": current})
    streaks.sort(key=lambda x: (-x["longest"], -x["current"]))

    conn.close()

    is_current_week = wk_start == _week_start(today)

    return templates.TemplateResponse(
        request,
        "three_hundred.html",
        {
            "players": players,
            "week_dates": week_dates,
            "day_headers": day_headers,
            "week_label": week_label,
            "grid": grid,
            "week_totals": week_totals,
            "all_time": all_time,
            "streaks": streaks,
            "goal": GOAL,
            "prev_week": (wk_start - timedelta(days=7)).isoformat(),
            "next_week": (wk_start + timedelta(days=7)).isoformat(),
            "this_week": _week_start(today).isoformat(),
            "is_current_week": is_current_week,
            "active": "threehundred",
        },
    )


@app.post("/threehundred/save")
async def save_shot_logs(request: Request):
    form = await request.form()
    week = form.get("week")
    wk_start = datetime.strptime(week, "%Y-%m-%d").date()
    week_dates = [wk_start + timedelta(days=i) for i in range(7)]

    conn = db.get_connection()
    players = conn.execute("SELECT id FROM players").fetchall()
    for p in players:
        for d in week_dates:
            raw = form.get(f"makes_{p['id']}_{d.isoformat()}")
            if raw is None or str(raw).strip() == "":
                conn.execute(
                    "DELETE FROM shot_logs WHERE player_id = ? AND log_date = ?",
                    (p["id"], d.isoformat()),
                )
                continue
            try:
                makes = int(raw)
            except ValueError:
                continue
            conn.execute(
                """INSERT INTO shot_logs (player_id, log_date, makes) VALUES (?, ?, ?)
                   ON CONFLICT(player_id, log_date) DO UPDATE SET makes=excluded.makes""",
                (p["id"], d.isoformat(), makes),
            )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/threehundred?week={week}", status_code=303)


# ---------- One-time 300 Club history import (remove after use) ----------
# Source: 300_Club.xlsx. Day order per player is Mon,Tue,Wed,Thu,Fri,Sat,Sun.
# None = not logged that day (no row created). 0 = explicitly logged as zero.

HISTORICAL_300CLUB = {
    "2026-06-01": {  # Week 1 (shooting started Tuesday this week)
        "Beau": [None, 331, 325, 300, 337, 300, 411],
        "Bin": [None, 300, 300, 300, 300, 0, 310],
        "Bryson": [None, 300, 300, 300, 315, 0, 0],
        "Cedric": [None, 300, 300, 300, 315, 0, 0],
        "CJ": [None, 308, 307, 300, 309, 304, 0],
        "Damarion": [None, 320, 300, 300, 300, 300, 0],
        "Dan": [None, 310, 300, 305, 320, 305, 0],
        "DJ": [None, None, None, None, None, None, None],
        "Daniel": [None, 300, 320, 300, 315, 0, 0],
        "Isaiah": [None, None, None, None, None, None, None],
        "Jacori": [None, 300, 325, 350, 300, 330, 300],
        "Jamal": [None, 300, 180, 300, 300, 0, 0],
    },
    "2026-06-08": {  # Week 2
        "Beau": [333, 400, 430, 484, 635, 542, 350],
        "Bin": [300, 310, 320, 300, 350, 0, 0],
        "Bryson": [350, 315, 300, 325, 0, 0, 0],
        "Cedric": [350, 400, 415, 415, 400, 0, 0],
        "CJ": [350, 328, 395, 335, 337, 347, 0],
        "Damarion": [350, 250, 315, 310, 320, 0, 0],
        "Dan": [310, 320, 305, 350, 320, 0, 0],
        "DJ": [None, None, None, None, None, None, None],
        "Daniel": [250, 430, 315, 0, 320, 0, 320],
        "Isaiah": [None, None, None, None, None, None, None],
        "Jacori": [375, 330, 310, 510, 405, 520, 310],
        "Jamal": [300, 300, 330, 0, 320, 0, 300],
    },
    "2026-06-15": {  # Week 3
        "Beau": [526, 535, 500, 550, 500, 600, 350],
        "Bin": [350, 350, 0, 350, None, 350, 300],
        "Bryson": [450, 400, 300, 0, None, 0, 0],
        "Cedric": [450, 450, 310, 0, None, 450, 350],
        "CJ": [316, 410, 206, 285, 365, 455, 0],
        "Damarion": [0, 300, 350, 350, 500, 450, 350],
        "Dan": [410, 350, 320, 310, None, 420, 270],
        "DJ": [None, None, None, None, None, None, None],
        "Daniel": [450, 450, 310, 0, 500, 350, 325],
        "Isaiah": [None, None, None, None, None, None, None],
        "Jacori": [530, 0, 850, 400, 350, 450, 350],
        "Jamal": [400, 200, 400, 0, None, 350, 0],
    },
    "2026-06-22": {  # Week 4
        "Beau": [550, 500, 400, 300, 300, 300, 300],
        "Bin": [360, 350, 0, 350, 0, 400, 340],
        "Bryson": [535, 335, 375, 425, 400, 450, 430],
        "Cedric": [400, 400, 0, 450, 400, 400, 400],
        "CJ": [337, 375, 0, 385, 320, 400, 300],
        "Damarion": [450, 400, 375, 400, 400, 450, 430],
        "Dan": [340, 320, 320, 350, 320, 0, 300],
        "DJ": [None, None, None, None, None, None, None],
        "Daniel": [350, 325, 350, 315, 350, 0, 330],
        "Isaiah": [None, None, None, None, None, 450, 450],
        "Jacori": [600, 400, 350, 350, 0, 300, 400],
        "Jamal": [400, 0, 300, 0, 500, 300, 0],
    },
    # Week of 2026-06-29 was skipped (no tracking that week).
    "2026-07-06": {  # Week 5
        "Beau": [450, 260, 370, 300, 410, 350, 0],
        "Bin": [300, 250, 300, 350, 300, 300, 0],
        "Bryson": [300, 350, 300, 300, 310, 0, 0],
        "Cedric": [300, 0, 300, 350, 0, 325, 0],
        "CJ": [325, 0, 300, 345, 375, 300, 0],
        "Damarion": [325, 300, 300, 325, 350, 350, 0],
        "Dan": [0, 0, 0, 0, 0, 0, 0],
        "DJ": [300, 400, 350, 350, 350, 250, 0],
        "Daniel": [0, 0, 0, 0, 0, 0, 0],
        "Isaiah": [0, 450, 0, 0, 0, 0, 0],
        "Jacori": [350, 0, 350, 375, 350, 300, 0],
        "Jamal": [300, 0, 300, 300, 0, 330, 0],
    },
    "2026-07-13": {  # Week 6
        "Beau": [410, 360, 300, 400, 0, 0, 350],
        "Bin": [0, 300, 300, 0, 300, 300, 0],
        "Bryson": [380, 300, 300, 0, 0, 0, 300],
        "Cedric": [375, 0, 0, 0, 0, 0, 0],
        "CJ": [325, 255, 0, 337, 345, 325, 325],
        "Damarion": [350, 0, 300, 300, 300, 350, 350],
        "Dan": [0, 0, 0, 0, 0, 0, 0],
        "DJ": [350, 450, 400, 450, 400, 0, 0],
        "Daniel": [0, 0, 0, 0, 0, 0, 0],
        "Isaiah": [415, 0, 0, 415, 415, 400, 400],
        "Jacori": [350, 400, 300, 400, 400, 400, 0],
        "Jamal": [0, 300, 300, 0, 300, 0, 400],
    },
}


@app.get("/admin/import-300club-history")
def import_300club_history():
    conn = db.get_connection()
    created_players = []
    matched_players = []
    rows_written = 0

    for week_start_str, players_data in HISTORICAL_300CLUB.items():
        wk_start = datetime.strptime(week_start_str, "%Y-%m-%d").date()
        week_dates = [wk_start + timedelta(days=i) for i in range(7)]

        for name, days in players_data.items():
            row = conn.execute("SELECT id FROM players WHERE name = ?", (name,)).fetchone()
            if row:
                player_id = row["id"]
                if name not in matched_players:
                    matched_players.append(name)
            else:
                cur = conn.execute(
                    "INSERT INTO players (name, status) VALUES (?, 'active')", (name,)
                )
                player_id = cur.lastrowid
                created_players.append(name)

            for d, makes in zip(week_dates, days):
                if makes is None:
                    continue
                conn.execute(
                    """INSERT INTO shot_logs (player_id, log_date, makes) VALUES (?, ?, ?)
                       ON CONFLICT(player_id, log_date) DO UPDATE SET makes=excluded.makes""",
                    (player_id, d.isoformat(), makes),
                )
                rows_written += 1

    conn.commit()
    conn.close()

    lines = [
        "300 Club history import complete.",
        f"Rows written: {rows_written}",
        f"Existing players matched: {sorted(set(matched_players)) or 'none'}",
        f"New players created: {sorted(set(created_players)) or 'none'}",
        "",
        "Go check /threehundred (use Previous week to page back through Jun 1 - Jul 19)"
        " and the All-time / Streaks leaderboards.",
    ]
    return Response("\n".join(lines), media_type="text/plain")
