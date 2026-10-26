"""Общие функции: настройки, база SQLite, даты, ошибки домена."""
import datetime as dt
import json
import os
import sqlite3
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "360.sqlite")

_db_lock = threading.RLock()


class BadRequest(Exception):
    """Неверный запрос (HTTP 400). Текст — сообщение пользователю."""


class Forbidden(Exception):
    """Нет прав (HTTP 403). Текст — сообщение пользователю."""


class NotFound(Exception):
    """Не найдено (HTTP 404). Текст — сообщение пользователю."""


class Conflict(Exception):
    """Конфликт состояния (HTTP 409). Текст — сообщение пользователю."""


def load_config():
    path = os.path.join(ROOT, "config.json")
    if not os.path.exists(path):
        path = os.path.join(ROOT, "config.example.json")
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    apply_timezone(config.get("timezone"))
    return config


def apply_timezone(name):
    """Часовой пояс процесса: от него зависят отметки времени, «сегодня» и сроки сессий.
    Нужен, когда сервер работает в UTC. На Windows не применяется — там берётся пояс системы."""
    if name and hasattr(time, "tzset") and os.environ.get("TZ") != name:
        os.environ["TZ"] = name
        time.tzset()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, fio TEXT NOT NULL, position TEXT NOT NULL DEFAULT '',
  login TEXT NOT NULL UNIQUE, pwd_hash TEXT NOT NULL, salt TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1, b24_user_id INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assessments (id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL, is_manager INTEGER NOT NULL,
  deadline TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_by INTEGER NOT NULL, created_at TEXT NOT NULL,
  closed_at TEXT);                                   -- status: active | finished | cancelled
CREATE TABLE IF NOT EXISTS assignments (id INTEGER PRIMARY KEY, assessment_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
  role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', sent_at TEXT NOT NULL, submitted_at TEXT,
  returned_at TEXT, b24_task_id INTEGER, UNIQUE (assessment_id, user_id));
                                                     -- status: draft | submitted | closed
CREATE TABLE IF NOT EXISTS answers (assignment_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY (assignment_id, key));
CREATE TABLE IF NOT EXISTS b24_ops (id INTEGER PRIMARY KEY, assignment_id INTEGER NOT NULL, op TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, done_at TEXT,
  created_at TEXT NOT NULL);                         -- op: create | deadline | comment | complete | renew
CREATE TABLE IF NOT EXISTS reminders (assignment_id INTEGER NOT NULL, day INTEGER NOT NULL, sent_at TEXT NOT NULL,
  PRIMARY KEY (assignment_id, day));
"""


def connect(path=None):
    if path is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = DB_PATH
    con = sqlite3.connect(path, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def db_lock():
    return _db_lock


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def today():
    return dt.date.today()
