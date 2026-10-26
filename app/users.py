"""Участники: создание, правка, список. Удаления нет — только отключение."""
import re
import sqlite3

from .auth import hash_password
from .common import BadRequest, Conflict, NotFound, db_lock, now

LOGIN_RE = re.compile(r"^[a-z0-9._-]{2,40}$")
MIN_PASSWORD = 6
FIELDS = ("fio", "position", "login", "password", "is_admin", "active", "b24_user_id")
PUBLIC_COLUMNS = "id, fio, position, login, is_admin, active, b24_user_id"


def _fio(value):
    if not isinstance(value, str) or not value.strip():
        raise BadRequest("Укажите ФИО")
    return value.strip()


def _position(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BadRequest("Должность должна быть текстом")
    return value.strip()


def _login(value):
    login = value.strip().lower() if isinstance(value, str) else ""
    if not LOGIN_RE.match(login):
        raise BadRequest("Логин: 2–40 символов, только латинские буквы, цифры, точка, дефис и подчёркивание")
    return login


def _password(value):
    if not isinstance(value, str) or len(value) < MIN_PASSWORD:
        raise BadRequest(f"Пароль должен быть не короче {MIN_PASSWORD} символов")
    return value


def _flag(value, name):
    if isinstance(value, bool) or value in (0, 1):
        return 1 if value else 0
    raise BadRequest(f"Поле «{name}» должно быть да или нет")


def _b24_user_id(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BadRequest("Пользователь Битрикс24: нужен положительный номер")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        n = int(value)
    else:
        raise BadRequest("Пользователь Битрикс24: нужен положительный номер")
    if n <= 0:
        raise BadRequest("Пользователь Битрикс24: нужен положительный номер")
    return n


def _row_to_dict(r):
    return {"id": r["id"], "fio": r["fio"], "position": r["position"], "login": r["login"],
            "is_admin": bool(r["is_admin"]), "active": bool(r["active"]), "b24_user_id": r["b24_user_id"]}


def create_user(con, fio, login, password, position="", is_admin=False, b24_user_id=None):
    fio, login, position = _fio(fio), _login(login), _position(position)
    _password(password)
    is_admin, b24_user_id = _flag(is_admin, "Администратор"), _b24_user_id(b24_user_id)
    h, salt = hash_password(password)
    with db_lock():
        if con.execute("SELECT 1 FROM users WHERE login=?", (login,)).fetchone():
            raise Conflict("Логин уже занят")
        cur = con.execute(
            "INSERT INTO users (fio, position, login, pwd_hash, salt, is_admin, active, b24_user_id, created_at) "
            "VALUES (?,?,?,?,?,?,1,?,?)", (fio, position, login, h, salt, is_admin, b24_user_id, now()))
        con.commit()
        return cur.lastrowid


def update_user(con, user_id, **fields):
    unknown = [k for k in fields if k not in FIELDS]
    if unknown:
        raise BadRequest(f"Неизвестное поле: {unknown[0]}")
    with db_lock():
        cur = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not cur:
            raise NotFound("Участник не найден")
        # сначала проверяем всё, потом пишем: ошибка в одном поле не оставляет частичной правки
        sets = {}
        if "fio" in fields:
            sets["fio"] = _fio(fields["fio"])
        if "position" in fields:
            sets["position"] = _position(fields["position"])
        if "login" in fields:
            login = _login(fields["login"])
            if con.execute("SELECT 1 FROM users WHERE login=? AND id<>?", (login, user_id)).fetchone():
                raise Conflict("Логин уже занят")
            sets["login"] = login
        if fields.get("password") not in (None, ""):
            sets["pwd_hash"], sets["salt"] = hash_password(_password(fields["password"]))
        if "is_admin" in fields:
            sets["is_admin"] = _flag(fields["is_admin"], "Администратор")
        if "active" in fields:
            sets["active"] = _flag(fields["active"], "Активен")
        if "b24_user_id" in fields:
            sets["b24_user_id"] = _b24_user_id(fields["b24_user_id"])
        # последний активный администратор остаётся администратором и остаётся активным
        was_admin = bool(cur["active"] and cur["is_admin"])
        will_be_admin = bool(sets.get("active", cur["active"]) and sets.get("is_admin", cur["is_admin"]))
        if was_admin and not will_be_admin and not con.execute(
                "SELECT 1 FROM users WHERE active=1 AND is_admin=1 AND id<>?", (user_id,)).fetchone():
            raise Conflict("Нельзя отключить или лишить прав последнего администратора")
        if not sets:
            return
        try:
            con.execute(f"UPDATE users SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                        (*sets.values(), user_id))
            con.commit()
        except sqlite3.IntegrityError:
            con.rollback()
            raise Conflict("Логин уже занят")


def list_users(con):
    # сортировка в Python: NOCASE в SQLite не различает регистр кириллицы
    rows = [_row_to_dict(r) for r in con.execute(f"SELECT {PUBLIC_COLUMNS} FROM users")]
    return sorted(rows, key=lambda r: (r["fio"].casefold(), r["id"]))


def get_user(con, user_id):
    r = con.execute(f"SELECT {PUBLIC_COLUMNS} FROM users WHERE id=?", (user_id,)).fetchone()
    if not r:
        raise NotFound("Участник не найден")
    return _row_to_dict(r)


def public(row):
    """Данные текущего пользователя для /api/me."""
    return {"id": row["id"], "fio": row["fio"], "position": row["position"], "login": row["login"],
            "is_admin": bool(row["is_admin"])}
