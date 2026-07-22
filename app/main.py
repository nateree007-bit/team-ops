import base64
import json
import os
import re
import secrets
import time

import httpx
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

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

# The team is in Corpus Christi (US Central). Railway's servers run in UTC,
# so "now"/"today" for anything user-facing must be computed in this zone,
# not the server's clock, or the dashboard's what's-next / today logic is
# hours off.
TEAM_TZ = ZoneInfo(os.environ.get("TEAM_TIMEZONE", "America/Chicago"))


def _now_local() -> datetime:
    return datetime.now(TEAM_TZ)


def _today_local() -> date:
    return _now_local().date()


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


def _fmt_dt(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%b %d, %I:%M %p").replace(" 0", " ")
    except ValueError:
        return iso_str


templates.env.filters["fmt_dt"] = _fmt_dt


# ---------- Dashboard ----------

@app.get("/")
def dashboard(request: Request):
    conn = db.get_connection()
    now_local = _now_local()
    today = now_local.date().isoformat()
    now_time = now_local.strftime("%H:%M")

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
        if ev_date == now_local.date():
            when = "Today"
        elif ev_date == now_local.date() + timedelta(days=1):
            when = "Tomorrow"
        else:
            when = f"{ev_date.strftime('%a')} {ev_date.month}/{ev_date.day}"
        next_event_label = f"{when} at {_fmt_time(next_event['event_time'])}"

    open_tasks = conn.execute(
        """SELECT * FROM tasks WHERE done = 0
           ORDER BY priority, (due_date IS NULL), due_date, created_at LIMIT 10"""
    ).fetchall()
    counts = {
        "players": conn.execute("SELECT COUNT(*) c FROM players").fetchone()["c"],
        "today_events": len(today_events),
        "open_tasks": conn.execute("SELECT COUNT(*) c FROM tasks WHERE done = 0").fetchone()["c"],
    }

    birthday_rows = conn.execute(
        """SELECT name, birthday FROM players WHERE birthday IS NOT NULL AND birthday != ''
           UNION ALL
           SELECT name, birthday FROM staff WHERE birthday IS NOT NULL AND birthday != ''"""
    ).fetchall()
    conn.close()

    today_date = now_local.date()
    upcoming_birthdays = []
    for row in birthday_rows:
        try:
            bday = datetime.strptime(row["birthday"], "%Y-%m-%d").date()
        except ValueError:
            continue
        next_occurrence = bday.replace(year=today_date.year)
        if next_occurrence < today_date:
            next_occurrence = next_occurrence.replace(year=today_date.year + 1)
        days_until = (next_occurrence - today_date).days
        if days_until == 0:
            when = "today! 🎉"
        elif days_until == 1:
            when = "tomorrow"
        else:
            when = f"in {days_until} days"
        upcoming_birthdays.append(
            (
                next_occurrence,
                {
                    "name": row["name"].split()[0],
                    "date_label": f"{next_occurrence.strftime('%a')} {next_occurrence.month}/{next_occurrence.day}",
                    "when": when,
                    "is_today": days_until == 0,
                },
            )
        )
    upcoming_birthdays.sort(key=lambda x: x[0])
    upcoming_birthdays = [b for _, b in upcoming_birthdays[:3]]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "today_events": today_events,
            "next_event": next_event,
            "next_event_label": next_event_label,
            "open_tasks": open_tasks,
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
    staff = conn.execute("SELECT * FROM staff ORDER BY created_at, name").fetchall()
    conn.close()
    return templates.TemplateResponse(
        request,
        "players.html",
        {"players": players, "staff": staff, "active": "players"},
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


# ---------- Coaches / Support Staff ----------

STAFF_ROLES = [
    "Head Coach",
    "Assistant Coach",
    "Athletic Trainer",
    "Strength & Performance",
    "Academics",
    "Manager",
    "Other",
]


@app.post("/staff/new")
def create_staff(
    name: str = Form(...),
    role: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    birthday: str = Form(""),
):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO staff (name, role, phone, email, birthday) VALUES (?, ?, ?, ?, ?)",
        (name, role, phone, email, birthday or None),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/players", status_code=303)


@app.get("/staff/{staff_id}")
def staff_detail(request: Request, staff_id: int):
    conn = db.get_connection()
    member = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
    conn.close()
    return templates.TemplateResponse(
        request,
        "staff_detail.html",
        {"member": member, "roles": STAFF_ROLES, "active": "players"},
    )


@app.post("/staff/{staff_id}/edit")
def edit_staff(
    staff_id: int,
    name: str = Form(...),
    role: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    birthday: str = Form(""),
):
    conn = db.get_connection()
    conn.execute(
        "UPDATE staff SET name=?, role=?, phone=?, email=?, birthday=? WHERE id=?",
        (name, role, phone, email, birthday or None, staff_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/players", status_code=303)


@app.post("/staff/{staff_id}/delete")
def delete_staff(staff_id: int):
    conn = db.get_connection()
    conn.execute("DELETE FROM staff WHERE id = ?", (staff_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/players", status_code=303)


# ---------- Schedule / Events ----------

@app.get("/schedule")
def list_schedule(request: Request, week: str = ""):
    today = _today_local()
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

Today's date is {_today_local().isoformat()}. The PDF's day headers (e.g. "Monday, July 20th") never
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


# ---------- To-Do ----------

COACHES = ["Shaw", "Davis", "Rencher", "Dylan", "JB", "Rob"]
STAFF = ["Nate", "Ashley", "Soza"]
MANAGERS = ["Tay", "Ty", "Kat", "Regan", "Joe"]


@app.get("/tasks")
def list_tasks(request: Request):
    conn = db.get_connection()
    todos = conn.execute(
        """SELECT * FROM tasks WHERE done = 0
           ORDER BY priority, (due_date IS NULL), due_date, created_at"""
    ).fetchall()
    graveyard = conn.execute(
        "SELECT * FROM tasks WHERE done = 1 ORDER BY completed_at DESC"
    ).fetchall()
    players = conn.execute(
        "SELECT name FROM players ORDER BY name"
    ).fetchall()
    conn.close()
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "todos": todos,
            "graveyard": graveyard,
            "coaches": COACHES,
            "staff": STAFF,
            "managers": MANAGERS,
            "players": [p["name"] for p in players],
            "active": "tasks",
        },
    )


@app.post("/tasks/new")
def create_task(
    title: str = Form(...),
    assignee: str = Form(...),
    description: str = Form(""),
    priority: int = Form(3),
    due_date: str = Form(""),
    add_to_schedule: str = Form(""),
):
    priority = max(1, min(5, priority))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO tasks (title, description, due_date, assignee, priority) VALUES (?, ?, ?, ?, ?)",
        (title, description, due_date or None, assignee, priority),
    )
    if add_to_schedule and due_date:
        conn.execute(
            """INSERT INTO events (type, title, event_date, notes)
               VALUES ('other', ?, ?, ?)""",
            (title, due_date, f"To-do for {assignee}" + (f" — {description}" if description else "")),
        )
    conn.commit()
    conn.close()
    return RedirectResponse("/tasks", status_code=303)


@app.post("/tasks/{task_id}/toggle")
def toggle_task(task_id: int, next: str = Form("/tasks")):
    if next not in ("/tasks", "/"):
        next = "/tasks"
    conn = db.get_connection()
    row = conn.execute("SELECT done FROM tasks WHERE id = ?", (task_id,)).fetchone()
    celebrating = False
    if row:
        if row["done"]:
            conn.execute(
                "UPDATE tasks SET done = 0, completed_at = NULL WHERE id = ?", (task_id,)
            )
        else:
            conn.execute(
                "UPDATE tasks SET done = 1, completed_at = ? WHERE id = ?",
                (_now_local().isoformat(timespec="seconds"), task_id),
            )
            celebrating = True
    conn.commit()
    conn.close()
    suffix = ("&" if "?" in next else "?") + "celebrate=1" if celebrating else ""
    return RedirectResponse(f"{next}{suffix}", status_code=303)


@app.post("/tasks/{task_id}/delete")
def delete_task(task_id: int):
    conn = db.get_connection()
    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/tasks", status_code=303)


# ---------- Film (Sportscode timeline stats) ----------

# Stat label names in the team's Sportscode code window.
FILM_STAT_EVENTS = {
    "+2", "-2", "+3", "-3", "FT Made", "FT Miss",
    "Assist", "Block", "Steal", "TO", "OREB", "DREB",
}

# Label-group codes the coders use that differ from display names.
FILM_PLAYER_ALIASES = {
    "BB": "Beau",
    "BW": "Bryson",
    "CE": "Cedric",
    "IL": "Isaiah",
    "JA": "Jamal",
    "DanielMich": "Daniel",
}


def _parse_sctimeline(raw: bytes):
    """Parse a .SCTimeline (JSON) export. Returns (session_date, events)
    where events are dicts of player/event/start/end for stat labels only."""
    data = json.loads(raw.decode("utf-8-sig"))
    timeline = data.get("timeline", data)
    session_date = None
    start = timeline.get("startTime")
    if start:
        try:
            session_date = datetime.fromisoformat(start.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            pass

    events = []
    for row in timeline.get("rows", []):
        row_name = (row.get("name") or "").strip()
        for inst in row.get("instances", []):
            for label in inst.get("labels", []):
                name = (label.get("name") or "").strip()
                if name not in FILM_STAT_EVENTS:
                    continue
                group = (label.get("group") or "").strip()
                player = FILM_PLAYER_ALIASES.get(group, group) or row_name or "Unknown"
                events.append(
                    {
                        "player": player,
                        "event": name,
                        "start": inst.get("startTime"),
                        "end": inst.get("endTime"),
                    }
                )
    return session_date, events


def _film_box_score(rows):
    """Aggregate film_events rows into per-player box score lines."""
    players: dict[str, dict] = {}
    for r in rows:
        p = players.setdefault(
            r["player"],
            {
                "player": r["player"],
                "twos_made": 0, "twos_att": 0,
                "threes_made": 0, "threes_att": 0,
                "ft_made": 0, "ft_att": 0,
                "oreb": 0, "dreb": 0,
                "ast": 0, "stl": 0, "blk": 0, "to": 0,
            },
        )
        e = r["event"]
        if e == "+2":
            p["twos_made"] += 1; p["twos_att"] += 1
        elif e == "-2":
            p["twos_att"] += 1
        elif e == "+3":
            p["threes_made"] += 1; p["threes_att"] += 1
        elif e == "-3":
            p["threes_att"] += 1
        elif e == "FT Made":
            p["ft_made"] += 1; p["ft_att"] += 1
        elif e == "FT Miss":
            p["ft_att"] += 1
        elif e == "OREB":
            p["oreb"] += 1
        elif e == "DREB":
            p["dreb"] += 1
        elif e == "Assist":
            p["ast"] += 1
        elif e == "Steal":
            p["stl"] += 1
        elif e == "Block":
            p["blk"] += 1
        elif e == "TO":
            p["to"] += 1

    lines = []
    for p in players.values():
        p["pts"] = 2 * p["twos_made"] + 3 * p["threes_made"] + p["ft_made"]
        p["fgm"] = p["twos_made"] + p["threes_made"]
        p["fga"] = p["twos_att"] + p["threes_att"]
        p["reb"] = p["oreb"] + p["dreb"]
        p["fg_pct"] = round(100 * p["fgm"] / p["fga"]) if p["fga"] else None
        p["three_pct"] = round(100 * p["threes_made"] / p["threes_att"]) if p["threes_att"] else None
        p["ft_pct"] = round(100 * p["ft_made"] / p["ft_att"]) if p["ft_att"] else None
        lines.append(p)
    lines.sort(key=lambda x: (-x["pts"], x["player"].lower()))

    totals = {
        k: sum(l[k] for l in lines)
        for k in ["pts", "fgm", "fga", "threes_made", "threes_att", "ft_made", "ft_att", "oreb", "dreb", "reb", "ast", "stl", "blk", "to"]
    }
    totals["fg_pct"] = round(100 * totals["fgm"] / totals["fga"]) if totals["fga"] else None
    totals["three_pct"] = round(100 * totals["threes_made"] / totals["threes_att"]) if totals["threes_att"] else None
    totals["ft_pct"] = round(100 * totals["ft_made"] / totals["ft_att"]) if totals["ft_att"] else None
    return lines, totals


@app.get("/film")
def list_film(request: Request):
    conn = db.get_connection()
    sessions = conn.execute(
        """SELECT film_sessions.*, COUNT(film_events.id) AS event_count
           FROM film_sessions LEFT JOIN film_events ON film_events.session_id = film_sessions.id
           GROUP BY film_sessions.id
           ORDER BY (session_date IS NULL), session_date DESC, created_at DESC"""
    ).fetchall()
    conn.close()
    return templates.TemplateResponse(
        request, "film.html", {"sessions": sessions, "active": "film"}
    )


@app.post("/film/upload")
async def upload_film(title: str = Form(""), file: UploadFile = File(...)):
    raw = await file.read()
    try:
        session_date, events = _parse_sctimeline(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError):
        return Response(
            "Couldn't parse that file as a Sportscode timeline (.SCTimeline). "
            "Make sure you're uploading the .SCTimeline file from inside the package.",
            media_type="text/plain",
            status_code=400,
        )

    clean_title = title.strip() or (file.filename or "Untitled session").rsplit(".", 1)[0]
    conn = db.get_connection()
    cur = conn.execute(
        "INSERT INTO film_sessions (title, session_date, source_file) VALUES (?, ?, ?)",
        (clean_title, session_date, file.filename),
    )
    session_id = cur.lastrowid
    for e in events:
        conn.execute(
            "INSERT INTO film_events (session_id, player, event, start_time, end_time) VALUES (?, ?, ?, ?, ?)",
            (session_id, e["player"], e["event"], e["start"], e["end"]),
        )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/film/{session_id}", status_code=303)


def _youtube_id(url):
    if not url:
        return None
    m = re.search(
        r"(?:youtube\.com/(?:watch\?[^#]*?v=|embed/|shorts/|live/)|youtu\.be/)"
        r"([A-Za-z0-9_-]{6,})",
        url,
    )
    return m.group(1) if m else None


def _probe_frameable(url):
    """Can this URL render inside an iframe on another site? Checks
    X-Frame-Options / CSP frame-ancestors and login redirects. Hudl's
    private team video fails this (login wall) and falls back to a link."""
    try:
        resp = httpx.get(
            url,
            timeout=10,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
    except Exception:
        return False
    if resp.status_code != 200:
        return False
    final_url = str(resp.url).lower()
    if "login" in final_url or "signin" in final_url:
        return False
    xfo = resp.headers.get("x-frame-options", "").lower()
    if "deny" in xfo or "sameorigin" in xfo:
        return False
    csp = resp.headers.get("content-security-policy", "").lower()
    if "frame-ancestors" in csp:
        allowed = csp.split("frame-ancestors", 1)[1].split(";")[0]
        if "*" not in allowed:
            return False
    return True


@app.post("/film/{session_id}/video")
def set_film_video(session_id: int, url: str = Form("")):
    url = url.strip()
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    if not url:
        kind = None
        url = None
    elif _youtube_id(url):
        kind = "youtube"
    elif _probe_frameable(url):
        kind = "iframe"
    else:
        kind = "link"
    conn = db.get_connection()
    conn.execute(
        "UPDATE film_sessions SET video_url = ?, video_kind = ? WHERE id = ?",
        (url, kind, session_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/film/{session_id}", status_code=303)


@app.get("/film/{session_id}")
def film_detail(request: Request, session_id: int):
    conn = db.get_connection()
    session = conn.execute(
        "SELECT * FROM film_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    rows = conn.execute(
        """SELECT player, event, start_time, end_time FROM film_events
           WHERE session_id = ? ORDER BY (start_time IS NULL), start_time""",
        (session_id,),
    ).fetchall()
    conn.close()
    lines, totals = _film_box_score(rows)
    clips = [
        {
            "player": r["player"],
            "event": r["event"],
            "start": r["start_time"],
            "end": r["end_time"],
        }
        for r in rows
        if r["start_time"] is not None
    ]
    return templates.TemplateResponse(
        request,
        "film_detail.html",
        {
            "session": session,
            "lines": lines,
            "totals": totals,
            "clips_json": json.dumps(clips).replace("</", "<\\/"),
            "has_clips": bool(clips),
            "youtube_id": _youtube_id(session["video_url"]) if session else None,
            "active": "film",
        },
    )


@app.post("/film/{session_id}/delete")
def delete_film(session_id: int):
    conn = db.get_connection()
    conn.execute("DELETE FROM film_sessions WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/film", status_code=303)


# ---------- Recruiting (embedded Google Sheet) ----------

def _get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _sheet_id(url):
    """Extract the spreadsheet id from a normal share link (not a
    published /d/e/2PACX... link, whose id is a publish token)."""
    if not url or "docs.google.com/spreadsheets" not in url or "/d/e/" in url:
        return None
    m = re.search(r"/spreadsheets/(?:u/\d+/)?d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def _sheet_embed_url(url, gid=None):
    """Turn any Google Sheets link into one that renders inside an iframe.
    Published-to-web links (/d/e/2PACX.../pubhtml) are used as-is; normal
    share links (/d/<id>/edit) get widget params appended. gid selects
    which worksheet tab opens."""
    if not url:
        return None
    if "docs.google.com/spreadsheets" not in url:
        return None
    if "/pubhtml" in url or "/d/e/" in url:
        base = url.split("?")[0]
        if "/pubhtml" not in base:
            base = base.rstrip("/") + "/pubhtml"
        extra = f"&gid={gid}&single=true" if gid else ""
        return base + "?widget=true&headers=false" + extra
    sheet_id = _sheet_id(url)
    if not sheet_id:
        return None
    if gid is None:
        gid_m = re.search(r"[#?&]gid=(\d+)", url)
        if gid_m:
            gid = gid_m.group(1)
    # Tab selection on /edit embeds uses the #gid= fragment, not a query param.
    frag = f"#gid={gid}" if gid else ""
    return (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
        f"?widget=true&headers=false&rm=minimal{frag}"
    )


def _js_unescape(s):
    s = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    return s.replace('\\/', "/").replace('\\"', '"').replace("\\\\", "\\")


_sheet_tabs_cache = {}
_SHEET_TABS_TTL = 300  # seconds


def _fetch_sheet_tabs(sheet_id):
    """Scrape the worksheet-tab names/gids from the sheet's htmlview page
    (available for any link-viewable sheet, no API key). Cached for a few
    minutes. Returns [] on any failure so the page falls back to a plain
    embed with Google's own tab bar."""
    cached = _sheet_tabs_cache.get(sheet_id)
    if cached and time.time() - cached[0] < _SHEET_TABS_TTL:
        return cached[1]
    tabs = []
    try:
        resp = httpx.get(
            f"https://docs.google.com/spreadsheets/d/{sheet_id}/htmlview",
            timeout=10,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            for name, gid in re.findall(
                r'items\.push\(\{name: "((?:\\.|[^"\\])*)",.*?gid: "(\d+)"',
                resp.text,
            ):
                tabs.append({"name": _js_unescape(name), "gid": gid})
    except Exception:
        tabs = []
    _sheet_tabs_cache[sheet_id] = (time.time(), tabs)
    return tabs


@app.get("/recruiting")
def recruiting(request: Request, invalid: str = "", gid: str = ""):
    conn = db.get_connection()
    sheet_url = _get_setting(conn, "recruiting_sheet_url")
    conn.close()

    sheet_id = _sheet_id(sheet_url)
    tabs = _fetch_sheet_tabs(sheet_id) if sheet_id else []
    known_gids = {t["gid"] for t in tabs}
    active_gid = gid if gid in known_gids else (tabs[0]["gid"] if tabs else None)

    return templates.TemplateResponse(
        request,
        "recruiting.html",
        {
            "active": "recruiting",
            "sheet_url": sheet_url,
            "embed_url": _sheet_embed_url(sheet_url, active_gid),
            "tabs": tabs,
            "active_gid": active_gid,
            "invalid": invalid == "1",
        },
    )


@app.post("/recruiting/link")
def set_recruiting_link(url: str = Form("")):
    url = url.strip()
    conn = db.get_connection()
    if not url:
        conn.execute("DELETE FROM settings WHERE key = 'recruiting_sheet_url'")
        conn.commit()
        conn.close()
        return RedirectResponse("/recruiting", status_code=303)
    if _sheet_embed_url(url) is None:
        conn.close()
        return RedirectResponse("/recruiting?invalid=1", status_code=303)
    _set_setting(conn, "recruiting_sheet_url", url)
    conn.commit()
    conn.close()
    return RedirectResponse("/recruiting", status_code=303)


# ---------- 300 Club ----------

GOAL = 300


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


@app.get("/threehundred")
def three_hundred(request: Request, week: str = ""):
    today = _today_local()
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
            "today": today.isoformat(),
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


_IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _build_shot_screenshot_prompt(players) -> str:
    roster = "\n".join(f"- id {p['id']}: {p['name']}" for p in players)
    return f"""You are reading screenshot(s) of a basketball team group chat where players text in how many
shots they made today for the "300 Club" (daily goal: 300 makes).

Team roster:
{roster}

Return ONLY a JSON array (no markdown fences, no commentary), one object per player report found:
- "sender": the display name shown in the chat for that message (use "me" for the phone owner's own
  messages, which usually appear on the right without a name)
- "makes": the number of makes reported, as an integer
- "player_id": the id of the matching roster player, or null if you can't confidently match

Rules:
- Chat names are often nicknames or first names — match to the roster by first name, last name, or an
  obvious nickname. If a name could match two roster players, use null.
- If the same person reports more than once, keep only their final/latest number.
- A report is a message whose main content is a shot count (e.g. "315", "made 302", "300 club ✅ 341").
  Ignore all other chatter, reactions, and questions.
- Numbers are typically between 0 and 1000. "300+12" style messages mean 312.
- Output strictly valid JSON. No trailing commas, no comments."""


@app.post("/threehundred/import-screenshot")
async def import_shot_screenshot(
    request: Request,
    files: list[UploadFile] = File(...),
    log_date: str = Form(""),
):
    if not ANTHROPIC_API_KEY:
        return Response(
            "ANTHROPIC_API_KEY is not configured on this server.", status_code=500
        )
    try:
        the_date = datetime.strptime(log_date, "%Y-%m-%d").date()
    except ValueError:
        the_date = _today_local()

    content = []
    for f in files:
        ext = os.path.splitext(f.filename or "")[1].lower()
        media_type = _IMAGE_MEDIA_TYPES.get(ext) or (
            f.content_type if f.content_type in _IMAGE_MEDIA_TYPES.values() else None
        )
        if not media_type:
            return Response(
                f"'{f.filename}' isn't a supported image. Please upload PNG or JPG "
                "screenshots (phone screenshots are usually PNG).",
                media_type="text/plain",
                status_code=400,
            )
        img_bytes = await f.read()
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(img_bytes).decode("utf-8"),
                },
            }
        )

    conn = db.get_connection()
    players = conn.execute("SELECT * FROM players ORDER BY name").fetchall()
    existing = {
        row["player_id"]: row["makes"]
        for row in conn.execute(
            "SELECT player_id, makes FROM shot_logs WHERE log_date = ?",
            (the_date.isoformat(),),
        )
    }
    conn.close()

    content.append({"type": "text", "text": _build_shot_screenshot_prompt(players)})
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": content}],
    )
    raw_text = message.content[0].text.strip()
    if raw_text.startswith("```"):
        lines = raw_text.split("\n")
        lines = lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]
        raw_text = "\n".join(lines)
    start = raw_text.find("[")
    end = raw_text.rfind("]")
    if start != -1 and end != -1:
        raw_text = raw_text[start : end + 1]
    try:
        reports = json.loads(raw_text)
    except json.JSONDecodeError:
        return Response(
            "Couldn't parse the AI's response as JSON. Raw output:\n\n" + raw_text,
            media_type="text/plain",
            status_code=500,
        )

    player_ids = {p["id"] for p in players}
    rows = []
    for r in reports:
        try:
            makes = int(r.get("makes"))
        except (TypeError, ValueError):
            continue
        pid = r.get("player_id")
        pid = pid if isinstance(pid, int) and pid in player_ids else None
        rows.append(
            {
                "sender": str(r.get("sender") or "?"),
                "makes": makes,
                "player_id": pid,
                "existing": existing.get(pid) if pid else None,
            }
        )

    return templates.TemplateResponse(
        request,
        "shots_import_preview.html",
        {
            "rows": rows,
            "players": players,
            "log_date": the_date.isoformat(),
            "date_label": the_date.strftime("%A, %b %d"),
            "active": "threehundred",
        },
    )


@app.post("/threehundred/import-screenshot/confirm")
async def confirm_shot_screenshot(request: Request):
    form = await request.form()
    log_date = form.get("log_date") or _today_local().isoformat()
    try:
        the_date = datetime.strptime(log_date, "%Y-%m-%d").date()
    except ValueError:
        the_date = _today_local()

    conn = db.get_connection()
    n = int(form.get("row_count") or 0)
    saved = 0
    for i in range(n):
        pid = form.get(f"player_{i}")
        makes_raw = form.get(f"makes_{i}")
        if not pid or makes_raw is None or str(makes_raw).strip() == "":
            continue
        try:
            pid = int(pid)
            makes = int(makes_raw)
        except ValueError:
            continue
        conn.execute(
            """INSERT INTO shot_logs (player_id, log_date, makes) VALUES (?, ?, ?)
               ON CONFLICT(player_id, log_date) DO UPDATE SET makes=excluded.makes""",
            (pid, the_date.isoformat(), makes),
        )
        saved += 1
    conn.commit()
    conn.close()
    week = _week_start(the_date).isoformat()
    return RedirectResponse(f"/threehundred?week={week}", status_code=303)


# ---------- One-time import: coaches/support staff from MBB Directory ----------
# Source: MBB Directory 2026-2027 copy.xlsx (Contact Sheet + Size Sheet roles).
# Remove this route after running it once.

STAFF_IMPORT = [
    ("Jim Shaw", "Head Coach", "(402) 560-1879", "jim.shaw@tamucc.edu", "1990-09-25"),
    ("Ralph Davis", "Assistant Coach", "(201) 965-2551", "ralph.davis@tamucc.edu", "1984-10-29"),
    ("Terrence Rencher", "Assistant Coach", "(512) 921-8917", "terrence.rencher@tamucc.edu", "1973-02-19"),
    ("Dylan Johnson", "Assistant Coach", "(618) 795-4177", "dylan.johnson@tamucc.edu", "1991-12-02"),
    ("Robert Edwards", "Assistant Coach", "(913) 907-0336", "robert.edwards@tamucc.edu", "1994-06-14"),
    ("Johnathan Bell", "Assistant Coach", "(818) 437-0142", "jonathan.finister-bell@tamucc.edu", "1993-09-21"),
    ("Ashley Myers", "Athletic Trainer", "(361) 446-1459", "ashley.myers@tamucc.edu", "1998-10-28"),
    ("Derick Soza", "Strength & Performance", "(956) 457-5507", "derick.soza@tamucc.edu", "1991-09-15"),
    ("Haley Blankinship", "Academics", "(703) 638-2562", "haley.blankinship@tamucc.edu", "1997-11-11"),
]


@app.get("/admin/import-staff")
def import_staff():
    conn = db.get_connection()
    added = []
    skipped = []
    for name, role, phone, email, birthday in STAFF_IMPORT:
        existing = conn.execute("SELECT id FROM staff WHERE name = ?", (name,)).fetchone()
        if existing:
            skipped.append(name)
            continue
        conn.execute(
            "INSERT INTO staff (name, role, phone, email, birthday) VALUES (?, ?, ?, ?, ?)",
            (name, role, phone, email, birthday),
        )
        added.append(name)
    conn.commit()
    conn.close()
    return Response(
        "Staff import complete.\n"
        f"Added: {added or 'none'}\n"
        f"Already existed (skipped): {skipped or 'none'}\n\n"
        "Check the Roster page's staff section and the dashboard birthday tracker.",
        media_type="text/plain",
    )
