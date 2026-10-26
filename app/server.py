"""Веб-сервер «Оценка 360°»: вход, API, статика."""
import collections
import datetime as dt
import json
import os
import re
import threading
import time
import traceback
import urllib.parse
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import assessments, auth, b24queue, users
from .b24queue import Worker
from .bitrix24 import B24Error, B24Tasks, Bitrix24
from .common import (ROOT, BadRequest, Conflict, Forbidden, NotFound, backup_db, connect, db_lock, load_config,
                     today)
from .questionnaire import EXTRA_TITLE, INSTRUCTION, OPEN_QUESTIONS, ROLES, SCALE, blocks_for
from .report import build_report

WEB = os.path.join(ROOT, "web")
COOKIE = "s360_session"
MAX_BODY = 1024 * 1024  # тело запроса больше 1 МБ — ошибка
B24_USERS_TTL = 10 * 60  # секунд: список пользователей портала держится в памяти
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BAD_REQUEST = "Неверный запрос"
BAD_LOGIN = "Неверный логин или пароль"
LOGIN_MAX_LEN = 40  # максимум в users.LOGIN_RE
BACKUP_FIRST_DELAY = 60  # секунд после запуска до первой копии базы
BACKUP_CHECK_PERIOD = 3600  # секунд: раз в столько проверяется, есть ли копия за сегодня
# единственные адреса статики: других путей нет, обойти каталог web/ нельзя
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

Attachment = collections.namedtuple("Attachment", "name data")

# (метод, шаблон пути, обработчик); {id} — целое не длиннее 18 цифр, иначе путь не найден (404)
_ID = r"([0-9]{1,18})"
ROUTES = [(m, re.compile("^" + p.replace("{id}", _ID) + "$"), h) for m, p, h in [
    ("GET", "/api/me", "me"),
    ("GET", "/api/my", "my"),
    ("GET", "/api/questionnaire", "questionnaire"),
    ("GET", "/api/assignments/{id}", "assignment"),
    ("POST", "/api/assignments/{id}/answer", "answer"),
    ("POST", "/api/assignments/{id}/submit", "submit"),
    ("GET", "/api/admin/users", "users_list"),
    ("POST", "/api/admin/users", "user_create"),
    ("POST", "/api/admin/users/{id}", "user_update"),
    ("GET", "/api/admin/b24-users", "b24_users"),
    ("GET", "/api/admin/assessments", "assessments_list"),
    ("POST", "/api/admin/assessments", "assessment_create"),
    ("GET", "/api/admin/assessments/{id}", "assessment_get"),
    ("POST", "/api/admin/assessments/{id}/respondents", "respondent_add"),
    ("POST", "/api/admin/assessments/{id}/deadline", "deadline_set"),
    ("POST", "/api/admin/assessments/{id}/finish", "assessment_finish"),
    ("POST", "/api/admin/assessments/{id}/cancel", "assessment_cancel"),
    ("GET", "/api/admin/assessments/{id}/xlsx", "xlsx"),
    ("POST", "/api/admin/assignments/{id}/remove", "assignment_remove"),
    ("POST", "/api/admin/assignments/{id}/return", "assignment_return"),
    ("POST", "/api/admin/assignments/{id}/retry", "assignment_retry"),
]]
# обработчики, которые сами ходят в сеть: общая блокировка базы на время вызова не берётся
UNLOCKED = {"b24_users"}


class HttpError(Exception):
    """Ответ с произвольным кодом (401, 429, 502). Текст — сообщение пользователю."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class App:
    def __init__(self, config, con):
        self.config, self.con = config, con
        # та же блокировка, под которой модули пишут в базу; она реентерабельная
        self.lock = db_lock()
        self.guard = auth.LoginGuard()
        b24 = config.get("bitrix24") or {}
        self.tasks = B24Tasks(Bitrix24(b24)) if b24.get("webhook") else None
        self.worker = Worker(con, self.lock, config, self.tasks)
        self.b24_users_cache = None  # (момент, список) — на B24_USERS_TTL секунд

    def session_hours(self):
        return self.config["server"].get("session_hours", 12)

    def secure_cookie(self):
        return bool(self.config["server"].get("secure_cookie"))


class Handler(BaseHTTPRequestHandler):
    app: App = None
    server_version = "Assessment360/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---------- ответы
    def send_json(self, obj, code=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("X-Content-Type-Options", "nosniff")
        # после обновления приложения браузер не должен брать из кеша старую страницу, скрипт или стили
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def send_attachment(self, att):
        """Файл .xlsx: кириллическое имя — только в filename* (RFC 5987), в filename — ASCII-запасное."""
        self.send_response(200)
        self.send_header("Content-Type", XLSX_TYPE)
        self.send_header("Content-Disposition", "attachment; filename=\"otsenka-360.xlsx\"; "
                         f"filename*=UTF-8''{urllib.parse.quote(att.name, safe='')}")
        self.send_header("Content-Length", str(len(att.data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(att.data)

    # ---------- запрос
    def client_ip(self):
        """Адрес клиента. За обратным прокси (server.trust_proxy) все запросы приходят с 127.0.0.1,
        поэтому адрес берётся из последнего элемента X-Forwarded-For — его дописывает сам прокси."""
        ip = self.client_address[0]
        if self.app.config["server"].get("trust_proxy") and ip in ("127.0.0.1", "::1"):
            return (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip() or ip
        return ip

    def token(self):
        try:
            c = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        return c[COOKIE].value if COOKIE in c else None

    def content_length(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            raise BadRequest(BAD_REQUEST)
        if n < 0:
            raise BadRequest(BAD_REQUEST)
        return n

    def drain(self, n):
        """Читает и отбрасывает тело: непрочитанное тело оборвало бы ответ сбросом соединения.
        Слишком большое тело (больше 4 МБ) не читаем — такое соединение пусть рвётся."""
        self.body_consumed = True
        left = n if n <= 4 * MAX_BODY else 0
        while left > 0:
            chunk = self.rfile.read(min(1 << 16, left))
            if not chunk:
                break
            left -= len(chunk)

    def require_json(self):
        """Тип application/json — на любом POST, и с пустым телом: форма с другого сайта JSON-тип отправить
        не может, а fetch с этим типом требует предварительного запроса CORS, который сервер не разрешает."""
        media_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if media_type != "application/json":
            raise BadRequest(BAD_REQUEST)

    def body(self):
        n = self.content_length()
        if n > MAX_BODY:
            self.drain(n)
            raise BadRequest(BAD_REQUEST)
        self.require_json()
        if not n:
            return {}
        self.body_consumed = True
        try:
            b = json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:  # и UnicodeDecodeError, и JSONDecodeError
            raise BadRequest(BAD_REQUEST)
        if not isinstance(b, dict):
            raise BadRequest(BAD_REQUEST)
        return b

    def cookie_header(self, token, expire=False):
        secure = "; Secure" if self.app.secure_cookie() else ""
        age = "; Max-Age=0" if expire else ""
        return f"{COOKIE}={token}{age}; HttpOnly; SameSite=Lax; Path=/{secure}"

    def do_GET(self):
        self.handle_request("GET")

    def do_POST(self):
        self.handle_request("POST")

    def handle_request(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        self.cookie = None
        self.body_consumed = False
        try:
            if path.startswith("/api/"):
                q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                result = self.api(method, path, q)
            elif method == "GET" and path in STATIC:
                name, ctype = STATIC[path]
                file = os.path.join(WEB, name)
                if not os.path.isfile(file):
                    raise NotFound("Не найдено")
                result = (file, ctype)
            else:
                raise NotFound("Не найдено")
        except HttpError as e:
            code, result = e.code, {"error": str(e)}
        except BadRequest as e:
            code, result = 400, {"error": str(e)}
        except Forbidden as e:
            code, result = 403, {"error": str(e)}
        except NotFound as e:
            code, result = 404, {"error": str(e)}
        except Conflict as e:
            code, result = 409, {"error": str(e)}
        except Exception:  # noqa: текст исключения пользователю не показываем — только в журнал
            traceback.print_exc()
            code, result = 500, {"error": "Ошибка сервера"}
        else:
            code = 200
        try:
            if not self.body_consumed:  # ответили раньше, чем разобрали тело (401, 403, 404, выход)
                self.drain(self.content_length())
        except (BadRequest, ConnectionError):
            pass
        try:
            if code != 200:
                self.send_json(result, code)
            elif isinstance(result, Attachment):
                self.send_attachment(result)
            elif isinstance(result, tuple):
                self.send_file(*result)
            else:
                self.send_json(result, cookie=self.cookie)
        except ConnectionError:
            pass  # браузер закрыл соединение, не дождавшись ответа

    # ---------- API
    def api(self, method, path, q):
        app = self.app
        if method == "POST" and path == "/api/login":
            return self.login()
        if method == "POST" and path == "/api/logout":
            self.require_json()  # тело выходу не нужно (дочитывается в handle_request), но тип — обязателен
            if self.token():
                auth.drop_session(app.con, self.token())
            self.cookie = self.cookie_header("", expire=True)
            return {"ok": True}
        # соединение с базой одно на все потоки: без блокировки параллельные запросы мешают друг другу
        with app.lock:
            row = auth.session_user(app.con, self.token())
        if not row:
            raise HttpError(401, "Требуется вход")
        me = dict(row)
        if path.startswith("/api/admin/") and not me["is_admin"]:
            raise Forbidden("Недостаточно прав")
        for m, rx, name in ROUTES:
            found = rx.match(path) if m == method else None
            if found:
                break
        else:
            raise NotFound("Не найдено")
        b = self.body() if method == "POST" else {}
        ids = [int(x) for x in found.groups()]
        fn = getattr(self, "h_" + name)
        if name in UNLOCKED:
            return fn(me, q, b, *ids)
        with app.lock:
            return fn(me, q, b, *ids)

    def login(self):
        app = self.app
        b = self.body()
        login = b.get("login")
        # такой логин не существует (users.LOGIN_RE: до 40 символов): отказ сразу, без записи в LoginGuard
        # (иначе анонимные запросы с огромными логинами копили бы память) и без хеширования
        if not isinstance(login, str) or len(login.strip()) > LOGIN_MAX_LEN:
            raise HttpError(401, BAD_LOGIN)
        key = (self.client_ip(), login.strip().lower())
        password = b.get("password")
        with app.lock:
            if app.guard.blocked(key):
                raise HttpError(429, "Слишком много неудачных попыток входа. Попробуйте через 15 минут")
            # попытка засчитывается сразу, до хеширования: иначе одновременные запросы прошли бы проверку
            # предела все вместе, пока ни один ещё не записал неудачу. Успешный вход снимает её через reset
            app.guard.failed(key)
            row = auth.login_row(app.con, login)
        # PBKDF2 — вне общей блокировки: иначе каждая попытка входа на доли секунды останавливала бы все запросы
        h = auth.attempt_hash(row, password)
        with app.lock:
            # пока считался хеш, пароль могли сменить, а учётную запись — отключить: сверяем с текущей строкой
            user = auth.login_row(app.con, login)
            if (not isinstance(password, str) or row is None or user is None or user["salt"] != row["salt"]
                    or not auth.hash_matches(user, h)):
                raise HttpError(401, BAD_LOGIN)  # неудача уже засчитана до хеширования
            app.guard.reset(key)
            token = auth.new_session(app.con, user["id"], app.session_hours())
        self.cookie = self.cookie_header(token)
        return {"ok": True}

    def wake(self):
        self.app.worker.wake()

    def h_me(self, me, q, b):
        cfg = self.app.config
        deadline = today() + dt.timedelta(days=cfg["default_deadline_days"])
        return {"user": users.public(me), "is_admin": bool(me["is_admin"]), "default_deadline": deadline.isoformat(),
                "roles": ROLES, "b24_enabled": self.app.tasks is not None}

    def h_my(self, me, q, b):
        return assessments.my_assignments(self.app.con, me["id"])

    def h_questionnaire(self, me, q, b):
        return {"instruction": INSTRUCTION, "scale": SCALE, "blocks": blocks_for(q.get("manager") == "1"),
                "open_questions": OPEN_QUESTIONS, "extra_title": EXTRA_TITLE}

    def h_assignment(self, me, q, b, assignment_id):
        return assessments.get_assignment(self.app.con, assignment_id, viewer=me)

    def h_answer(self, me, q, b, assignment_id):
        assessments.save_answer(self.app.con, assignment_id, me["id"], b.get("key"), b.get("value"))
        return {"ok": True}

    def h_submit(self, me, q, b, assignment_id):
        assessments.submit(self.app.con, assignment_id, me["id"])
        self.wake()
        return {"ok": True}

    # ---------- администратор
    def h_users_list(self, me, q, b):
        return users.list_users(self.app.con)

    def h_user_create(self, me, q, b):
        new_id = users.create_user(self.app.con, b.get("fio"), b.get("login"), b.get("password"),
                                   position=b.get("position"), is_admin=b.get("is_admin", False),
                                   b24_user_id=b.get("b24_user_id"))
        return {"id": new_id}

    def h_user_update(self, me, q, b, user_id):
        unknown = [k for k in b if k not in users.FIELDS]
        if unknown:  # лишний ключ не должен попасть в **fields (например, «con»)
            raise BadRequest(f"Неизвестное поле: {unknown[0]}")
        users.update_user(self.app.con, user_id, **b)
        return {"ok": True}

    def h_b24_users(self, me, q, b):
        """Сетевой вызов — вне блокировки базы; результат держится в памяти 10 минут."""
        app = self.app
        if app.tasks is None:
            return []
        with app.lock:
            cache = app.b24_users_cache
        if q.get("refresh") != "1" and cache and time.monotonic() - cache[0] < B24_USERS_TTL:
            return cache[1]
        try:
            found = app.tasks.users()
        except B24Error as e:
            raise HttpError(502, f"Битрикс24 недоступен: {e}")
        with app.lock:
            app.b24_users_cache = (time.monotonic(), found)
        return found

    def h_assessments_list(self, me, q, b):
        return assessments.list_assessments(self.app.con)

    def h_assessment_create(self, me, q, b):
        respondents = b.get("respondents")
        if isinstance(respondents, list):
            if any(not isinstance(r, dict) for r in respondents):
                raise BadRequest("Оценивающий — пара «участник, роль»")
            respondents = [(r.get("user_id"), r.get("role")) for r in respondents]
        new_id = assessments.create_assessment(self.app.con, b.get("subject_id"), b.get("is_manager", False),
                                               b.get("deadline"), me["id"], respondents)
        self.wake()
        return {"id": new_id}

    def h_assessment_get(self, me, q, b, assessment_id):
        return assessments.get_assessment(self.app.con, assessment_id)

    def h_respondent_add(self, me, q, b, assessment_id):
        new_id = assessments.add_respondent(self.app.con, assessment_id, b.get("user_id"), b.get("role"))
        self.wake()
        return {"assignment_id": new_id}

    def h_deadline_set(self, me, q, b, assessment_id):
        assessments.set_deadline(self.app.con, assessment_id, b.get("deadline"))
        self.wake()
        return {"ok": True}

    def h_assessment_finish(self, me, q, b, assessment_id):
        assessments.finish_assessment(self.app.con, assessment_id)
        self.wake()
        return {"ok": True}

    def h_assessment_cancel(self, me, q, b, assessment_id):
        assessments.cancel_assessment(self.app.con, assessment_id)
        self.wake()
        return {"ok": True}

    def h_xlsx(self, me, q, b, assessment_id):
        return Attachment(*build_report(self.app.con, assessment_id))

    def h_assignment_remove(self, me, q, b, assignment_id):
        assessments.remove_respondent(self.app.con, assignment_id)
        self.wake()
        return {"ok": True}

    def h_assignment_return(self, me, q, b, assignment_id):
        assessments.return_for_revision(self.app.con, assignment_id)
        self.wake()
        return {"ok": True}

    def h_assignment_retry(self, me, q, b, assignment_id):
        if not self.app.con.execute("SELECT 1 FROM assignments WHERE id=?", (assignment_id,)).fetchone():
            raise NotFound("Анкета не найдена")
        b24queue.retry(self.app.con, assignment_id)
        self.wake()
        return {"ok": True}


def make_server(app, host, port):
    handler = type("BoundHandler", (Handler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)


def backup_daily(con):
    """Фоновый поток: копия базы раз в день (data/backups), независимо от Битрикс24. Первая — вскоре после
    запуска, если за сегодня копии ещё нет; дальше проверка раз в час. Сбой пишется в журнал и не
    останавливает приложение."""
    time.sleep(BACKUP_FIRST_DELAY)
    while True:
        try:
            made = backup_db(con)
            if made:
                print(f"Копия базы: {made}")
        except Exception:
            traceback.print_exc()
        time.sleep(BACKUP_CHECK_PERIOD)


def serve():
    config = load_config()
    app = App(config, connect())
    srv = config["server"]
    host, port = srv.get("host", "127.0.0.1"), int(srv.get("port", 8082))
    try:
        httpd = make_server(app, host, port)
    except OSError as e:
        print(f"Не удалось занять порт {port}: {e}. Возможно, приложение уже запущено в другом окне "
              f"или порт занят другой программой — поменяйте server.port в config.json.")
        return
    if not app.con.execute("SELECT 1 FROM users WHERE is_admin=1 AND active=1").fetchone():
        print("Администраторов нет. Создайте администратора: python manage.py create-admin ЛОГИН")
    if app.worker.start():
        print("Битрикс24: задачи и напоминания включены")
    else:
        print("Битрикс24: адрес вебхука не задан в config.json — задачи не создаются")
    threading.Thread(target=backup_daily, args=(app.con,), name="db-backup", daemon=True).start()
    url = f"http://localhost:{port}"
    print(f"Оценка 360° работает: {url}  (Ctrl+C или закрытие окна — остановить)")
    if srv.get("open_browser", True):
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.worker.stop()
        httpd.server_close()
