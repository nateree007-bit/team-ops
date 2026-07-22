import os
import sqlite3
from pathlib import Path

_DATA_DIR = os.environ.get("DATA_DIR")
if _DATA_DIR:
    DB_PATH = Path(_DATA_DIR) / "team.db"
else:
    DB_PATH = Path(__file__).resolve().parent.parent / "data" / "team.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    jersey_number TEXT,
    position TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT,
    birthday TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    event_date TEXT NOT NULL,
    event_time TEXT,
    event_time_end TEXT,
    location TEXT,
    opponent TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS game_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    minutes INTEGER DEFAULT 0,
    points INTEGER DEFAULT 0,
    rebounds INTEGER DEFAULT 0,
    assists INTEGER DEFAULT 0,
    steals INTEGER DEFAULT 0,
    blocks INTEGER DEFAULT 0,
    turnovers INTEGER DEFAULT 0,
    fouls INTEGER DEFAULT 0,
    notes TEXT,
    UNIQUE(event_id, player_id)
);

CREATE TABLE IF NOT EXISTS scouting_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opponent TEXT NOT NULL,
    event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
    content TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shot_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    log_date TEXT NOT NULL,
    makes INTEGER NOT NULL DEFAULT 0,
    UNIQUE(player_id, log_date)
);

CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT,
    phone TEXT,
    email TEXT,
    birthday TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS film_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    session_date TEXT,
    source_file TEXT,
    video_url TEXT,
    video_kind TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS film_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES film_sessions(id) ON DELETE CASCADE,
    player TEXT NOT NULL,
    event TEXT NOT NULL,
    start_time REAL,
    end_time REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    due_date TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    assignee TEXT,
    priority INTEGER DEFAULT 3,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(players)")}
        if "birthday" not in columns:
            conn.execute("ALTER TABLE players ADD COLUMN birthday TEXT")
        event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
        if "event_time_end" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN event_time_end TEXT")
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "assignee" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN assignee TEXT")
        if "priority" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 3")
        if "completed_at" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT")
        film_columns = {row["name"] for row in conn.execute("PRAGMA table_info(film_sessions)")}
        if "video_url" not in film_columns:
            conn.execute("ALTER TABLE film_sessions ADD COLUMN video_url TEXT")
            conn.execute("ALTER TABLE film_sessions ADD COLUMN video_kind TEXT")
        conn.commit()
    finally:
        conn.close()
