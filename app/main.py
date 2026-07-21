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


def _fmt_birthday(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.strptime(iso_str, "%Y-%m-%d").strftime("%b %d, %Y")
    except ValueError:
        return iso_str


templates.env.filters["fmt_birthday"] = _fmt_birthday


def _fmt_time(hhmm):
    if not hhmm:
        return None
    try:
        return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return hhmm


templates.env.filters["fmt_time"] = _fmt_time


# ---------- Dashboard ----------

@app.get("/")
def dashboard(request: Request):
    conn = db.get_connection()
    today = date.today().isoformat()
    now_time = datetime.now().strftime("%H:%M")

    today_rows = conn.execute(
        "SELECT * FROM events WHERE event_date = ? ORDER BY (event_time IS NULL), event_time",
        (today,),
    ).fetchall()
    today_events = []
    for e in today_rows:
        status = None
        if e["event_time"]:
            end = e["event_time_end"] or e["event_time"]
            if now_time < e["event_time"]:
                status = "upcoming"
            elif now_time > end:
                status = "past"
            else:
                status = "current"
        today_events.append({"event": e, "status": status})

    next_event = conn.execute(
        """SELECT * FROM events
           WHERE event_time IS NOT NULL
             AND (event_date > ? OR (event_date = ? AND event_time > ?))
           ORDER BY event_date, event_time LIMIT 1""",
        (today, today, now_time),
    ).fetchone()
    next_event_label = None
    if next_event:
        ev_date = datetime.strptime(next_event["event_date"], "%Y-%m-%d").date()
        if ev_date == date.today():
            when = "Today"
        elif ev_date == date.today() + timedelta(days=1):
            when = "Tomorrow"
        else:
            when = f"{ev_date.strftime('%a')} {ev_date.month}/{ev_date.day}"
        next_event_label = f"{when} at {_fmt_time(next_event['event_time'])}"

    open_tasks = conn.execute(
        "SELECT * FROM tasks WHERE done = 0 ORDER BY (due_date IS NULL), due_date LIMIT 8"
    ).fetchall()
    injured = conn.execute(
        "SELECT * FROM players WHERE status != 'active' ORDER BY name"
    ).fetchall()
    counts = {
        "players": conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"],
        "today_events": len(today_events),
        "open_tasks": conn.execute("SELECT COUNT(*) c FROM tasks WHERE done = 0").fetchone()["c"],
    }

    birthday_rows = conn.execute(
        "SELECT name, birthday FROM players WHERE birthday IS NOT NULL AND birthday != ''"
    ).fetchall()
    conn.close()

    today_date = date.today()
    upcoming_birthdays = []
    for row in birthday_rows:
        try:
            bday = datetime.strptime(row["birthday"], "%Y-%m-%d").date()
        except ValueError:
            continue
        next_occurrence = bday.replace(year=today_date.year)
        if next_occurrence < today_date:
            next_occurrence = next_occurrence.replace(year=today_date.year + 1)
        first_name = row["name"].split()[0]
        label = f"{first_name} {next_occurrence.strftime('%a')} {next_occurrence.month}/{next_occurrence.day}"
        upcoming_birthdays.append((next_occurrence, label))
    upcoming_birthdays.sort(key=lambda x: x[0])
    upcoming_birthdays = [label for _, label in upcoming_birthdays[:3]]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "today_events": today_events,
            "next_event": next_event,
            "next_event_label": next_event_label,
            "open_tasks": open_tasks,
            "injured": injured,
            "counts": counts,
            "upcoming_birthdays": upcoming_birthdays,
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
    birthday: str = Form(""),
    notes: str = Form(""),
):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO players (name, jersey_number, position, status, birthday, notes) VALUES (?, ?, ?, ?, ?, ?)",
        (name, jersey_number, position, status, birthday or None, notes),
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
    birthday: str = Form(""),
    notes: str = Form(""),
):
    conn = db.get_connection()
    conn.execute(
        "UPDATE players SET name=?, jersey_number=?, position=?, status=?, birthday=?, notes=? WHERE id=?",
        (name, jersey_number, position, status, birthday or None, notes, player_id),
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
def list_schedule(request: Request, week: str = ""):
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
    week_label = f"{wk_start.strftime('%b')} {wk_start.day} – {wk_end.strftime('%b')} {wk_end.day}, {wk_end.year}"

    conn = db.get_connection()
    rows = conn.execute(
        """SELECT * FROM events WHERE event_date BETWEEN ? AND ?
           ORDER BY event_date, (event_time IS NULL), event_time""",
        (week_dates[0].isoformat(), wk_end.isoformat()),
    ).fetchall()
    conn.close()

    by_date: dict[str, list] = {}
    for r in rows:
        by_date.setdefault(r["event_date"], []).append(r)

    days = [
        {
            "date": d,
            "label": f"{d.strftime('%a')} {d.month}/{d.day}",
            "events": by_date.get(d.isoformat(), []),
            "is_today": d == today,
        }
        for d in week_dates
    ]

    return templates.TemplateResponse(
        request,
        "schedule.html",
        {
            "days": days,
            "week_label": week_label,
            "prev_week": (wk_start - timedelta(days=7)).isoformat(),
            "next_week": (wk_start + timedelta(days=7)).isoformat(),
            "this_week": _week_start(today).isoformat(),
            "is_current_week": wk_start == _week_start(today),
            "active": "schedule",
        },
    )


@app.post("/schedule/new")
def create_event(
    type: str = Form(...),
    title: str = Form(...),
    event_date: str = Form(...),
    event_time: str = Form(""),
    event_time_end: str = Form(""),
    location: str = Form(""),
    opponent: str = Form(""),
    notes: str = Form(""),
):
    conn = db.get_connection()
    conn.execute(
        """INSERT INTO events (type, title, event_date, event_time, event_time_end, location, opponent, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (type, title, event_date, event_time or None, event_time_end or None, location, opponent, notes),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/schedule?week={_week_start(datetime.strptime(event_date, '%Y-%m-%d').date()).isoformat()}", status_code=303)


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
    event_time_end: str = Form(""),
    location: str = Form(""),
    opponent: str = Form(""),
    notes: str = Form(""),
):
    conn = db.get_connection()
    conn.execute(
        """UPDATE events SET type=?, title=?, event_date=?, event_time=?, event_time_end=?, location=?, opponent=?, notes=?
           WHERE id=?""",
        (type, title, event_date, event_time or None, event_time_end or None, location, opponent, notes, event_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/schedule/{event_id}", status_code=303)


@app.post("/schedule/{event_id}/delete")
def delete_event(event_id: int):
    conn = db.get_connection()
    row = conn.execute("SELECT event_date FROM events WHERE id = ?", (event_id,)).fetchone()
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()
    week = _week_start(datetime.strptime(row["event_date"], "%Y-%m-%d").date()).isoformat() if row else ""
    return RedirectResponse(f"/schedule?week={week}", status_code=303)


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


# ---------- One-time import: this week's schedule from the PDF ----------
# Source: "Monday, July 20th - Sunday, July 26th .pdf" (Men's Basketball Weekly Schedule)
# (type, title, event_date, event_time, event_time_end, location, notes)
# Remove this route after running it once.

WEEKLY_SCHEDULE_IMPORT = [
    ("meal", "Breakfast", "2026-07-20", "08:00", "08:15", None, "Full Team"),
    ("conditioning", "Conditioning", "2026-07-20", "08:30", None, "Field House", "Full Team"),
    ("other", "Women's Team in the weight room", "2026-07-20", "09:00", "10:00", "Weight Room", None),
    ("weights", "Weights", "2026-07-20", "10:00", None, None, "Full Team"),
    ("other", "Women's Team on the court", "2026-07-20", "10:00", "12:00", "Court", None),
    ("meal", "Lunch", "2026-07-20", "12:30", "12:50", "Dining Hall", "Full Team"),
    ("study_hall", "Study Hall (CCH)", "2026-07-20", "13:00", "14:00", "CCH", "DJ, Isaiah, Daniel, Khi"),
    ("practice", "Small Group", "2026-07-20", "14:30", None, None, "Dan, Foon, Bin, Beau, Winston, Sebastian, Cedric, Jordan"),
    ("practice", "Small Group", "2026-07-20", "15:30", None, None, "Jamal, Khi, CJ, Isaiah, Bryson, Jacori, DJ, Daniel"),

    ("meal", "Breakfast", "2026-07-21", "08:00", "08:15", None, "Full Team"),
    ("conditioning", "Conditioning", "2026-07-21", "08:30", None, "Field House", "Full Team"),
    ("weights", "Weights", "2026-07-21", "09:00", None, None, "Full Team"),
    ("other", "Women's Team on the court", "2026-07-21", "10:00", "12:00", "Court", None),
    ("meal", "Lunch", "2026-07-21", "12:30", "12:50", "Dining Hall", "Full Team"),
    ("study_hall", "Study Hall (CCH)", "2026-07-21", "13:00", "14:00", "CCH", "DJ, Isaiah, Daniel, Khi"),
    ("practice", "Small Group", "2026-07-21", "14:30", None, None, "Dan, Foon, Bin, Beau, Winston, Sebastian, Cedric, Jordan"),
    ("practice", "Small Group", "2026-07-21", "15:30", None, None, "Jamal, Khi, CJ, Isaiah, Bryson, Jacori, DJ, Daniel"),

    ("other", "Women's Team in the weight room", "2026-07-22", "09:00", "10:00", "Weight Room", None),
    ("other", "Women's Team on the court", "2026-07-22", "10:00", "12:00", "Court", None),
    ("meal", "Lunch", "2026-07-22", "12:30", "12:50", "Dining Hall", "Full Team"),
    ("study_hall", "Study Hall (CCH)", "2026-07-22", "13:30", "14:30", "CCH", "DJ, Isaiah, Daniel, Khi"),
    ("other", "Team Arrives at Field House", "2026-07-22", "16:45", None, "Field House", "Full Team"),
    ("meeting", "Meet and Greet", "2026-07-22", "17:00", None, "Field House", "Full Team"),
    ("practice", "Open Practice Program (1 Hour of Basketball)", "2026-07-22", "17:30", "18:30", "Field House", "Full Team"),
    ("meeting", "Mingle with Guest", "2026-07-22", "18:45", None, "Field House", "Full Team"),

    ("conditioning", "Conditioning", "2026-07-23", "07:00", None, "Soccer Field", "Full Team"),
    ("other", "Women's Team in the weight room", "2026-07-23", "09:00", "10:00", "Weight Room", None),
    ("weights", "Weights", "2026-07-23", "10:00", None, None, "Full Team"),
    ("other", "Women's Team on the court", "2026-07-23", "10:00", "12:00", "Court", None),
    ("meal", "Lunch", "2026-07-23", "12:30", "12:50", "Dining Hall", "Full Team"),
    ("study_hall", "Study Hall (CCH)", "2026-07-23", "13:00", "14:00", "CCH", "DJ, Isaiah, Daniel, Khi"),
    ("practice", "Small Group", "2026-07-23", "14:30", None, None, "Dan, Foon, Bin, Beau, Winston, Sebastian, Cedric, Jordan"),
    ("practice", "Small Group", "2026-07-23", "15:30", None, None, "Jamal, Khi, CJ, Isaiah, Bryson, Jacori, DJ, Daniel"),

    ("conditioning", "Conditioning", "2026-07-24", "06:00", None, "Dugan Soccer Field", "Full Team"),
    ("meal", "TACOS!!!!", "2026-07-24", "06:30", None, None, None),
    ("other", "Women's Volleyball Camp in Field House", "2026-07-24", "07:00", "17:00", "Field House", None),
    ("study_hall", "Study Hall – If Necessary", "2026-07-24", "10:00", None, None, None),
    ("meeting", "Life Skills Event", "2026-07-24", "11:00", None, "UC 320", "Full Team"),
    ("meal", "Lunch", "2026-07-24", "12:30", "12:50", "Dining Hall", "Full Team"),
    ("other", "Team Activity - TBD", "2026-07-24", "13:00", None, None, None),

    ("other", "OFF DAY", "2026-07-25", None, None, None, None),
    ("other", "Women's Volleyball Camp in Field House", "2026-07-25", "07:00", "17:00", "Field House", None),

    ("other", "OFF DAY", "2026-07-26", None, None, None, None),
    ("other", "Women's Volleyball Camp in Field House", "2026-07-26", "07:00", "17:00", "Field House", None),
    ("practice", "Open Gym", "2026-07-26", "17:30", None, None, None),
    ("study_hall", "Study Hall – If Necessary", "2026-07-26", "19:00", None, None, None),
]


@app.get("/admin/import-weekly-schedule")
def import_weekly_schedule():
    conn = db.get_connection()
    for event_type, title, event_date, event_time, event_time_end, location, notes in WEEKLY_SCHEDULE_IMPORT:
        conn.execute(
            """INSERT INTO events (type, title, event_date, event_time, event_time_end, location, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_type, title, event_date, event_time, event_time_end, location, notes),
        )
    conn.commit()
    conn.close()
    return Response(
        f"Weekly schedule import complete. {len(WEEKLY_SCHEDULE_IMPORT)} events added for Jul 20-26, 2026.\n"
        "Check /schedule and the dashboard.",
        media_type="text/plain",
    )
