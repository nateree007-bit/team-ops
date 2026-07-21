import base64
import json
import os
import secrets
from datetime import date, datetime, timedelta

import anthropic
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from . import db

AUTH_USER = os.environ.get("TEAM_OPS_USER")
AUTH_PASSWORD = os.environ.get("TEAM_OPS_PASSWORD")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")


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


def _build_schedule_import_prompt() -> str:
    return f"""You are extracting a structured weekly schedule from a basketball team's schedule PDF.

Today's date is {date.today().isoformat()}. The PDF's day headers (e.g. "Monday, July 20th") never
state a year — use today's date to infer the correct year for every event (the schedule always refers
to a week at or near the current date, not a past year).

Return ONLY a JSON array (no markdown fences, no commentary before or after) of event objects with
exactly these fields:
- "type": one of "practice", "conditioning", "weights", "meal", "study_hall", "meeting", "game", "travel", "other"
- "title": short event title (e.g. "Breakfast", "Conditioning", "Small Group")
- "date": ISO date YYYY-MM-DD, computed from the day headers in the PDF
- "start_time": 24-hour "HH:MM", or null if no time is given (e.g. "OFF DAY")
- "end_time": 24-hour "HH:MM", or null if no end time is given
- "location": short location string, or null
- "notes": extra detail such as who's involved, or null

Rules:
- Include EVERY line item in the document for every day, including informational or "FYI" lines about
  other teams or facility usage (e.g. "Women's Team in the weight room until 10:00 AM", "Women's
  Volleyball Camp in Field House"). These affect the team's own scheduling and facility availability,
  so they matter even though they aren't the team's own activity. Use type "other" for these.
- Infer AM/PM from context when it isn't explicit (e.g. a time between two PM-labeled times is probably PM).
- "until X" or a time range means both start_time and end_time should be set.
- Items with no time at all (like "OFF DAY") should have start_time and end_time both null.
- Keep title short; put extra detail (attendee names, "if necessary", etc.) in notes.
- Output strictly valid JSON. No trailing commas, no comments.
"""


@app.post("/schedule/import-pdf")
async def import_schedule_pdf(request: Request, file: UploadFile = File(...)):
    if not ANTHROPIC_API_KEY:
        return Response(
            "ANTHROPIC_API_KEY is not configured on this server.", status_code=500
        )

    pdf_bytes = await file.read()
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": _build_schedule_import_prompt()},
                ],
            }
        ],
    )
    raw_text = message.content[0].text.strip()

    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        if lines[-1].strip().startswith("```"):
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        raw_text = "\n".join(lines)
    start = raw_text.find("[")
    end = raw_text.rfind("]")
    if start != -1 and end != -1:
        raw_text = raw_text[start : end + 1]

    try:
        events = json.loads(raw_text)
    except json.JSONDecodeError:
        return Response(
            "Couldn't parse the AI's response as JSON. Raw output:\n\n" + raw_text,
            media_type="text/plain",
            status_code=500,
        )

    return templates.TemplateResponse(
        request,
        "schedule_import_preview.html",
        {"events": events, "events_json": json.dumps(events), "active": "schedule"},
    )


@app.post("/schedule/import-pdf/confirm")
def confirm_import_schedule_pdf(events_json: str = Form(...)):
    events = json.loads(events_json)
    conn = db.get_connection()
    for e in events:
        conn.execute(
            """INSERT INTO events (type, title, event_date, event_time, event_time_end, location, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                e.get("type") or "other",
                e.get("title") or "Untitled",
                e["date"],
                e.get("start_time"),
                e.get("end_time"),
                e.get("location"),
                e.get("notes"),
            ),
        )
    conn.commit()
    conn.close()
    week = (
        _week_start(datetime.strptime(events[0]["date"], "%Y-%m-%d").date()).isoformat()
        if events
        else ""
    )
    return RedirectResponse(f"/schedule?week={week}", status_code=303)


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
