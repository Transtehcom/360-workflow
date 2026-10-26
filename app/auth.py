"""Пароли, защита от подбора и сессии."""
import datetime as dt
import hashlib
import hmac
import secrets
import time

from .common import db_lock, now

LOGIN_MAX_FAILS = 10
LOGIN_WINDOW = 15 * 60


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), 200_000).hex()
    return h, salt


class LoginGuard:
    """Не больше LOGIN_MAX_FAILS неудачных попыток за LOGIN_WINDOW секунд на пару (IP, логин)."""

    def __init__(self):
        self.fails = {}  # (ip, login) -> [время неудачных попыток]

    def blocked(self, key):
        t = time.monotonic()
        fails = [x for x in self.fails.get(key, []) if t - x < LOGIN_WINDOW]
        if fails:
            self.fails[key] = fails
        else:
            self.fails.pop(key, None)
        return len(fails) >= LOGIN_MAX_FAILS

    def failed(self, key):
        self.fails.setdefault(key, []).append(time.monotonic())

    def reset(self, key):
        self.fails.pop(key, None)


def check_password(con, login, password):
    """Пользователь, если пароль верен и учётная запись активна; иначе None. Логин — без учёта регистра."""
    if not isinstance(login, str) or not isinstance(password, str):
        return None
    r = con.execute("SELECT * FROM users WHERE login=? AND active=1", (login.strip().lower(),)).fetchone()
    if not r:
        return None
    h, _ = hash_password(password, r["salt"])
    return r if hmac.compare_digest(h, r["pwd_hash"]) else None


def new_session(con, user_id, hours):
    token = secrets.token_urlsafe(32)
    exp = (dt.datetime.now() + dt.timedelta(hours=hours)).isoformat(timespec="seconds")
    with db_lock():
        con.execute("INSERT INTO sessions (token, user_id, expires) VALUES (?,?,?)", (token, user_id, exp))
        con.execute("DELETE FROM sessions WHERE expires < ?", (now(),))
        con.commit()
    return token


def session_user(con, token):
    """Строка пользователя по токену; None — сессии нет, она просрочена или пользователь отключён."""
    if not token:
        return None
    return con.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token=? AND s.expires >= ? AND u.active=1", (token, now())).fetchone()


def drop_session(con, token):
    with db_lock():
        con.execute("DELETE FROM sessions WHERE token=?", (token,))
        con.commit()
