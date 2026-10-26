import datetime as dt
import http.client
import http.cookiejar
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth, server, users  # noqa: E402
from app.bitrix24 import B24Error  # noqa: E402
from app.common import ROOT, connect, today  # noqa: E402
from app.questionnaire import score_keys  # noqa: E402

PASSWORD = "secret1"


def make_config():
    with open(os.path.join(ROOT, "config.example.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["bitrix24"]["webhook"] = ""  # без сети: очередь Битрикс24 не работает
    return cfg


class Client:
    """Браузер одного пользователя: свои cookie, ответы без исключений на коды ошибок."""

    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method, path, body=None, headers=None):
        """(код, заголовки, тело в байтах)."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        h = {"Content-Type": "application/json"} if method == "POST" else {}  # тип нужен на любом POST, и без тела
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(req) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.headers, e.read()
            finally:
                e.close()

    def get(self, path):
        code, headers, data = self.call("GET", path)
        return code, json.loads(data.decode("utf-8"))

    def post(self, path, body=None):
        code, headers, data = self.call("POST", path, {} if body is None else body)
        return code, json.loads(data.decode("utf-8"))

    def login(self, login, password=PASSWORD):
        return self.post("/api/login", {"login": login, "password": password})


class ServerTestCase(unittest.TestCase):
    cfg_patch = None

    def setUp(self):
        self.cfg = make_config()
        if self.cfg_patch:
            self.cfg["server"].update(self.cfg_patch)
        self.con = connect(":memory:")
        self.app = server.App(self.cfg, self.con)
        self.httpd = server.make_server(self.app, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.nat = users.create_user(self.con, "Наталья", "nat", PASSWORD, is_admin=True)
        self.ivanov = users.create_user(self.con, "Иванов Пётр", "ivanov", PASSWORD)
        self.petrov = users.create_user(self.con, "Петров Иван", "petrov", PASSWORD)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        self.con.close()

    def client(self, login=None):
        c = Client(self.port)
        if login:
            code, _ = c.login(login)
            self.assertEqual(code, 200)
        return c

    def deadline(self, days=14):
        return (today() + dt.timedelta(days=days)).isoformat()

    def create_assessment(self, admin, respondents=None, is_manager=False):
        respondents = respondents or [{"user_id": self.ivanov, "role": "self"}, {"user_id": self.petrov, "role": "peer"}]
        code, r = admin.post("/api/admin/assessments", {
            "subject_id": self.ivanov, "is_manager": is_manager, "deadline": self.deadline(), "respondents": respondents})
        self.assertEqual(code, 200, r)
        return r["id"]

    def assignment_id(self, user_id):
        return self.con.execute("SELECT id FROM assignments WHERE user_id=?", (user_id,)).fetchone()["id"]


class AuthTest(ServerTestCase):
    def test_login_logout_and_401(self):
        c = Client(self.port)
        self.assertEqual(c.get("/api/me")[0], 401)
        self.assertEqual(c.login("nat", "wrong-password")[0], 401)
        code, headers, data = c.call("POST", "/api/login", {"login": "NAT", "password": PASSWORD})
        self.assertEqual(code, 200)
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith("s360_session="))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("Secure", cookie)
        token = cookie.split(";")[0].split("=", 1)[1]
        self.assertEqual(c.get("/api/me")[0], 200)
        code, headers, _ = c.call("POST", "/api/logout", {})
        self.assertEqual(code, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(c.get("/api/me")[0], 401)
        # сессия удалена и на сервере, а не только в cookie браузера
        code, _, _ = Client(self.port).call("GET", "/api/me", headers={"Cookie": "s360_session=" + token})
        self.assertEqual(code, 401)

    def test_login_lockout_429(self):
        c = Client(self.port)
        for _ in range(10):
            self.assertEqual(c.login("nat", "wrong-password")[0], 401)
        code, r = c.login("nat")  # верный пароль уже не помогает
        self.assertEqual(code, 429)
        self.assertIn("15 минут", r["error"])
        self.assertEqual(Client(self.port).login("ivanov")[0], 200)  # другой логин не заблокирован

    def test_inactive_user_cannot_log_in(self):
        users.update_user(self.con, self.petrov, active=False)
        self.assertEqual(Client(self.port).login("petrov")[0], 401)

    def test_me(self):
        code, me = self.client("ivanov").get("/api/me")
        self.assertEqual(code, 200)
        self.assertEqual(me["user"], {"id": self.ivanov, "fio": "Иванов Пётр", "position": "", "login": "ivanov",
                                      "is_admin": False})
        self.assertFalse(me["is_admin"])
        self.assertEqual(me["default_deadline"], self.deadline(self.cfg["default_deadline_days"]))
        self.assertEqual(me["roles"]["peer"], "коллега")
        self.assertFalse(me["b24_enabled"])
        self.assertTrue(self.client("nat").get("/api/me")[1]["is_admin"])

    def test_malformed_body_is_400(self):
        c = Client(self.port)
        for raw in (b"{oops", b"[1, 2]", b"\xff\xfe"):
            req = urllib.request.Request(c.base + "/api/login", data=raw, method="POST",
                                         headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                c.opener.open(req)
            self.assertEqual(cm.exception.code, 400)
            self.assertEqual(json.loads(cm.exception.read()), {"error": "Неверный запрос"})
            cm.exception.close()

    def test_oversized_body_is_400(self):
        c = Client(self.port)
        big = {"login": "nat", "password": "x" * (server.MAX_BODY + 10)}
        code, headers, data = c.call("POST", "/api/login", big)
        self.assertEqual(code, 400)
        self.assertEqual(json.loads(data)["error"], "Неверный запрос")

    def test_trust_proxy_uses_forwarded_ip(self):
        self.app.config["server"]["trust_proxy"] = True
        c = Client(self.port)
        for _ in range(10):
            code, _, _ = c.call("POST", "/api/login", {"login": "nat", "password": "bad-password"},
                                {"X-Forwarded-For": "10.0.0.1"})
            self.assertEqual(code, 401)
        code, _, _ = c.call("POST", "/api/login", {"login": "nat", "password": PASSWORD}, {"X-Forwarded-For": "10.0.0.1"})
        self.assertEqual(code, 429)
        code, _, _ = c.call("POST", "/api/login", {"login": "nat", "password": PASSWORD}, {"X-Forwarded-For": "10.0.0.2"})
        self.assertEqual(code, 200)

    def test_login_case_variants_share_one_lockout_counter(self):
        c = Client(self.port)
        for i in range(10):
            self.assertEqual(c.login("Ivanov" if i % 2 else "IVANOV", "wrong-password")[0], 401)
        self.assertEqual(c.login("ivanov")[0], 429)
        self.assertEqual(len(self.app.guard.fails), 1)

    def test_forwarded_ip_ignored_without_trust_proxy(self):
        self.assertFalse(self.cfg["server"].get("trust_proxy"))
        c = Client(self.port)
        for i in range(10):  # поддельный X-Forwarded-For не заводит отдельный счётчик
            code, _, _ = c.call("POST", "/api/login", {"login": "nat", "password": "bad-password"},
                                {"X-Forwarded-For": f"10.0.0.{i}"})
            self.assertEqual(code, 401)
        code, _, _ = c.call("POST", "/api/login", {"login": "nat", "password": PASSWORD}, {"X-Forwarded-For": "10.9.9.9"})
        self.assertEqual(code, 429)
        self.assertEqual(len(self.app.guard.fails), 1)

    def lock_is_free(self):
        """Свободна ли общая блокировка для другого потока (RLock своего потока взялся бы всегда)."""
        result = []

        def probe():
            result.append(self.app.lock.acquire(timeout=2))
            if result[0]:
                self.app.lock.release()

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        return result[0]

    def test_password_hash_is_outside_the_lock(self):
        free, real = [], auth.hash_password

        def probe(password, salt=None):
            free.append(self.lock_is_free())
            return real(password, salt)

        c = Client(self.port)
        with mock.patch("app.auth.hash_password", side_effect=probe):
            self.assertEqual(c.login("nat")[0], 200)
            self.assertEqual(c.login("nat", "wrong-password")[0], 401)
            self.assertEqual(c.login("nobody", "wrong-password")[0], 401)
        # и для несуществующего логина хеш считается — с фиктивной солью: время ответа то же
        self.assertEqual(free, [True, True, True])

    def test_password_changed_while_hashing_is_rejected(self):
        changed, real = [], auth.hash_password

        def probe(password, salt=None):
            result = real(password, salt)
            if not changed:  # пароль сменили, пока считался хеш попытки входа
                changed.append(1)
                users.update_user(self.con, self.petrov, password="другой-пароль")
            return result

        with mock.patch("app.auth.hash_password", side_effect=probe):
            self.assertEqual(Client(self.port).login("petrov")[0], 401)
        self.assertEqual(Client(self.port).login("petrov", "другой-пароль")[0], 200)

    def test_oversized_and_non_string_logins_leave_no_trace(self):
        c = Client(self.port)
        with mock.patch("app.auth.hash_password") as check:
            for i in range(30):
                code, r = c.login(f"{i}" + "x" * 100_000, "bad-password")
                self.assertEqual((code, r), (401, {"error": "Неверный логин или пароль"}))
            for login in ("x" * 41, " " + "x" * 41, ["nat"], {"a": 1}, 5, None):
                self.assertEqual(c.login(login, "bad-password")[0], 401, repr(login))
            check.assert_not_called()  # без хеширования
        self.assertEqual(self.app.guard.fails, {})
        # логин предельной длины (40) ещё идёт обычным путём и считается в защите от подбора
        self.assertEqual(c.login("x" * 40, "bad-password")[0], 401)
        self.assertEqual(list(self.app.guard.fails), [("127.0.0.1", "x" * 40)])

    def test_content_type_required_on_every_post(self):
        c = Client(self.port)
        code, headers, _ = c.call("POST", "/api/login", {"login": "petrov", "password": PASSWORD})
        cookie = headers["Set-Cookie"].split(";")[0]
        self.create_assessment(self.client("nat"))
        gid = self.assignment_id(self.petrov)

        def raw_post(path, body=b"{}", content_type=None, with_cookie=True):
            h = {"Cookie": cookie} if with_cookie else {}
            if content_type:
                h["Content-Type"] = content_type
            conn = http.client.HTTPConnection("127.0.0.1", self.port)
            try:
                conn.request("POST", path, body=body, headers=h)
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read().decode("utf-8"))
            finally:
                conn.close()

        for ctype in (None, "text/plain", "application/x-www-form-urlencoded", "text/plain; charset=application/json",
                      "application/jsonx", "multipart/form-data; boundary=application/json"):
            for body in (b"{}", b""):  # и пустое тело: submit, finish, cancel и другие действия без полей
                self.assertEqual(raw_post(f"/api/assignments/{gid}/submit", body, ctype),
                                 (400, {"error": "Неверный запрос"}), (ctype, body))
            self.assertEqual(raw_post("/api/login", b'{"login": "nat", "password": "secret1"}', ctype, False),
                             (400, {"error": "Неверный запрос"}), ctype)
        # с верным типом запрос доходит до обработчика (анкета не заполнена — это уже другая ошибка)
        code, r = raw_post(f"/api/assignments/{gid}/submit", b"", "application/json")
        self.assertEqual(code, 400)
        self.assertIn("Не заполнены", r["error"])
        self.assertEqual(raw_post(f"/api/assignments/{gid}/answer", b'{"key": "A1", "value": 3}',
                                  "Application/JSON; charset=utf-8"), (200, {"ok": True}))
        # и выход: иначе форма с другого сайта разлогинила бы участника
        for ctype in (None, "text/plain", "application/x-www-form-urlencoded"):
            self.assertEqual(raw_post("/api/logout", b"", ctype), (400, {"error": "Неверный запрос"}), ctype)
        self.assertEqual(c.get("/api/me")[0], 200)  # сессия цела
        self.assertEqual(raw_post("/api/logout", b"", "application/json"), (200, {"ok": True}))  # тело может быть пустым
        self.assertEqual(c.get("/api/me")[0], 401)

    def test_unread_body_is_drained_before_early_responses(self):
        big = b"x" * 900_000  # меньше предела тела, но больше буферов сокета
        member = Client(self.port)
        _, headers, _ = member.call("POST", "/api/login", {"login": "petrov", "password": PASSWORD})
        session = headers["Set-Cookie"].split(";")[0]
        for path, cookie, expected in (("/api/admin/users", "", 401), ("/api/nothing", "", 401),
                                       ("/api/admin/users", session, 403), ("/api/nothing", session, 404),
                                       ("/api/logout", session, 200)):
            conn = http.client.HTTPConnection("127.0.0.1", self.port)
            try:
                h = {"Content-Type": "application/json"}
                if cookie:
                    h["Cookie"] = cookie
                conn.request("POST", path, body=big, headers=h)
                resp = conn.getresponse()  # ответ дочитывается, а не обрывается сбросом соединения
                resp.read()
                self.assertEqual(resp.status, expected, path)
            finally:
                conn.close()


class SecureCookieTest(ServerTestCase):
    cfg_patch = {"secure_cookie": True}

    def test_secure_flag(self):
        code, headers, _ = Client(self.port).call("POST", "/api/login", {"login": "nat", "password": PASSWORD})
        self.assertEqual(code, 200)
        self.assertIn("; Secure", headers["Set-Cookie"])


class AccessTest(ServerTestCase):
    def test_admin_routes_forbidden_for_respondent(self):
        admin = self.client("nat")
        aid = self.create_assessment(admin)
        c = self.client("petrov")
        self.assertEqual(c.get("/api/admin/users")[0], 403)
        self.assertEqual(c.call("GET", f"/api/admin/assessments/{aid}/xlsx")[0], 403)
        self.assertEqual(c.post("/api/admin/users", {"fio": "X", "login": "xx", "password": PASSWORD})[0], 403)
        self.assertEqual(c.post(f"/api/admin/assessments/{aid}/finish")[0], 403)
        self.assertEqual(c.get("/api/admin/nothing")[0], 403)

    def test_respondent_cannot_read_foreign_assignment(self):
        admin = self.client("nat")
        self.create_assessment(admin)
        mine, foreign = self.assignment_id(self.petrov), self.assignment_id(self.ivanov)
        c = self.client("petrov")
        self.assertEqual(c.get(f"/api/assignments/{mine}")[0], 200)
        self.assertEqual(c.get(f"/api/assignments/{foreign}")[0], 404)
        self.assertEqual(c.post(f"/api/assignments/{foreign}/answer", {"key": "A1", "value": 3})[0], 404)
        self.assertEqual(c.post(f"/api/assignments/{foreign}/submit")[0], 404)

    def test_unknown_paths_and_ids_are_404(self):
        c = self.client("nat")
        for path in ("/api/nothing", "/api/assignments/abc", "/api/assignments/%D9%A1", "/api/admin/assessments/x",
                     "/api/assignments/99999999999999999999999", "/api/assignments/1/unknown"):
            code, r = c.get(path)
            self.assertEqual(code, 404, path)
            self.assertIn("error", r)
        self.assertEqual(c.post("/api/admin/assessments/abc/finish")[0], 404)
        self.assertEqual(c.post("/api/me")[0], 404)  # метод не тот
        self.assertEqual(Client(self.port).get("/api/nothing")[0], 401)

    def test_unexpected_error_is_500_without_details(self):
        c = self.client("nat")
        with mock.patch("app.server.assessments.list_assessments", side_effect=RuntimeError("secret detail")), \
                mock.patch("traceback.print_exc") as printed:
            code, r = c.get("/api/admin/assessments")
        self.assertEqual(code, 500)
        self.assertEqual(r, {"error": "Ошибка сервера"})
        printed.assert_called_once()


class FlowTest(ServerTestCase):
    def fill(self, c, assignment_id, is_manager=False):
        for key in score_keys(is_manager):
            self.assertEqual(c.post(f"/api/assignments/{assignment_id}/answer", {"key": key, "value": 4})[0], 200)

    def test_full_flow(self):
        admin = self.client("nat")
        aid = self.create_assessment(admin)
        petrov = self.client("petrov")
        code, mine = petrov.get("/api/my")
        self.assertEqual(code, 200)
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["subject_fio"], "Иванов Пётр")
        gid = mine[0]["assignment_id"]
        code, qn = petrov.get("/api/questionnaire?manager=0")
        self.assertEqual(code, 200)
        self.assertNotIn("M", [b["key"] for b in qn["blocks"]])
        self.assertIn("M", [b["key"] for b in petrov.get("/api/questionnaire?manager=1")[1]["blocks"]])
        self.assertTrue(qn["instruction"] and qn["scale"] and qn["open_questions"] and qn["extra_title"])
        self.fill(petrov, gid)
        code, g = petrov.get(f"/api/assignments/{gid}")
        self.assertEqual(code, 200)
        self.assertTrue(g["editable"])
        self.assertEqual(g["answers"]["A1"], "4")
        self.assertEqual(petrov.post(f"/api/assignments/{gid}/submit")[0], 200)
        self.assertEqual(petrov.get("/api/my"), (200, []))
        code, headers, data = admin.call("GET", f"/api/admin/assessments/{aid}/xlsx")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], server.XLSX_TYPE)
        disposition = headers["Content-Disposition"]
        self.assertIn("attachment", disposition)
        self.assertIn("filename*=UTF-8''", disposition)
        self.assertTrue(disposition.isascii())
        self.assertIn('filename="otsenka-360.xlsx"', disposition)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))
        code, a = admin.get(f"/api/admin/assessments/{aid}")
        self.assertEqual((a["submitted"], a["total"]), (1, 2))

    def test_admin_management(self):
        admin = self.client("nat")
        code, r = admin.post("/api/admin/users", {"fio": "Сидорова Анна", "position": "Бухгалтер", "login": "sidorova",
                                                  "password": PASSWORD, "is_admin": False, "b24_user_id": 7})
        self.assertEqual(code, 200)
        uid = r["id"]
        self.assertEqual(admin.post("/api/admin/users", {"fio": "Другая", "login": "sidorova", "password": PASSWORD})[0], 409)
        self.assertEqual(admin.post(f"/api/admin/users/{uid}", {"position": "Главный бухгалтер"})[0], 200)
        self.assertEqual(admin.post(f"/api/admin/users/{uid}", {"con": 1})[0], 400)
        self.assertEqual(admin.post("/api/admin/users/99999", {"fio": "X"})[0], 404)
        listed = {u["login"]: u for u in admin.get("/api/admin/users")[1]}
        self.assertEqual(listed["sidorova"]["position"], "Главный бухгалтер")
        self.assertNotIn("pwd_hash", listed["sidorova"])
        aid = self.create_assessment(admin)
        self.assertEqual(admin.get("/api/admin/assessments")[1][0]["id"], aid)
        code, r = admin.post(f"/api/admin/assessments/{aid}/respondents", {"user_id": uid, "role": "client"})
        self.assertEqual(code, 200)
        self.assertEqual(admin.post(f"/api/admin/assessments/{aid}/respondents", {"user_id": uid, "role": "client"})[0], 400)
        self.assertEqual(admin.post(f"/api/admin/assessments/{aid}/deadline", {"deadline": self.deadline(30)})[0], 200)
        self.assertEqual(admin.get(f"/api/admin/assessments/{aid}")[1]["deadline"], self.deadline(30))
        gid = r["assignment_id"]
        self.assertEqual(admin.post(f"/api/admin/assignments/{gid}/remove")[0], 200)
        self.assertEqual(admin.post(f"/api/admin/assignments/{gid}/remove")[0], 404)
        petrov_gid = self.assignment_id(self.petrov)
        self.assertEqual(admin.post(f"/api/admin/assignments/{petrov_gid}/return")[0], 409)  # ещё не отправлена
        self.assertEqual(admin.post(f"/api/admin/assignments/{petrov_gid}/retry")[0], 200)
        self.assertEqual(admin.post("/api/admin/assignments/99999/retry")[0], 404)
        self.assertEqual(admin.post(f"/api/admin/assessments/{aid}/finish")[0], 200)
        self.assertEqual(admin.post(f"/api/admin/assessments/{aid}/cancel")[0], 409)
        self.assertEqual(self.client("petrov").get("/api/my"), (200, []))

    def test_password_change_ends_sessions(self):
        admin, petrov = self.client("nat"), self.client("petrov")
        self.assertEqual(admin.post(f"/api/admin/users/{self.petrov}", {"password": "новый-пароль"})[0], 200)
        self.assertEqual(petrov.get("/api/me")[0], 401)
        self.assertEqual(admin.get("/api/me")[0], 200)
        # свой пароль: закрываются все сессии, включая текущую, — интерфейс покажет вход
        self.assertEqual(admin.post(f"/api/admin/users/{self.nat}", {"password": "новый-пароль"})[0], 200)
        self.assertEqual(admin.get("/api/me")[0], 401)
        self.assertEqual(admin.login("nat", "новый-пароль")[0], 200)

    def test_return_for_revision(self):
        admin = self.client("nat")
        self.create_assessment(admin)
        petrov, gid = self.client("petrov"), self.assignment_id(self.petrov)
        self.fill(petrov, gid)
        petrov.post(f"/api/assignments/{gid}/submit")
        self.assertEqual(admin.post(f"/api/admin/assignments/{gid}/return")[0], 200)
        self.assertEqual(len(petrov.get("/api/my")[1]), 1)

    def test_expired_session_keeps_answers(self):
        admin = self.client("nat")
        self.create_assessment(admin)
        petrov, gid = self.client("petrov"), self.assignment_id(self.petrov)
        self.assertEqual(petrov.post(f"/api/assignments/{gid}/answer", {"key": "A1", "value": 5})[0], 200)
        self.con.execute("UPDATE sessions SET expires='2000-01-01T00:00:00'")
        self.con.commit()
        self.assertEqual(petrov.post(f"/api/assignments/{gid}/answer", {"key": "A2", "value": 3})[0], 401)
        self.assertEqual(petrov.login("petrov")[0], 200)
        code, g = petrov.get(f"/api/assignments/{gid}")
        self.assertEqual(code, 200)
        self.assertEqual(g["answers"], {"A1": "5"})

    def test_errors_are_json(self):
        admin = self.client("nat")
        self.create_assessment(admin)
        petrov, gid = self.client("petrov"), self.assignment_id(self.petrov)
        code, r = petrov.post(f"/api/assignments/{gid}/submit")
        self.assertEqual(code, 400)
        self.assertIn("Не заполнены блоки", r["error"])
        self.assertEqual(petrov.post(f"/api/assignments/{gid}/answer", {"key": "A1", "value": 9})[0], 400)
        self.assertEqual(petrov.post(f"/api/assignments/{gid}/answer", {"key": ["x"], "value": 1})[0], 400)
        self.assertEqual(petrov.post(f"/api/assignments/{gid}/answer", {})[0], 400)
        self.fill(petrov, gid)
        self.assertEqual(petrov.post(f"/api/assignments/{gid}/submit")[0], 200)
        code, r = petrov.post(f"/api/assignments/{gid}/submit")
        self.assertEqual(code, 409)
        self.assertIn("error", r)
        self.assertEqual(petrov.post(f"/api/assignments/{gid}/answer", {"key": "A1", "value": 3})[0], 409)

    def test_create_assessment_validation(self):
        admin = self.client("nat")
        body = {"subject_id": self.ivanov, "is_manager": False, "deadline": self.deadline(), "respondents": []}
        self.assertEqual(admin.post("/api/admin/assessments", body)[0], 400)
        self.assertEqual(admin.post("/api/admin/assessments", {**body, "respondents": ["x"]})[0], 400)
        self.assertEqual(admin.post("/api/admin/assessments", {**body, "respondents": [{"user_id": 1, "role": "boss"}]})[0], 400)
        self.assertEqual(admin.post("/api/admin/assessments", {})[0], 400)

    def test_wrong_value_types_are_400_not_500(self):
        admin = self.client("nat")
        aid = self.create_assessment(admin)
        gid = self.assignment_id(self.petrov)
        junk = [["x"], {"a": 1}, 1.5, True, None]
        for v in junk:
            for path, body in (
                    ("/api/admin/users", {"fio": v, "login": v, "password": v, "is_admin": v, "b24_user_id": v}),
                    (f"/api/admin/users/{self.petrov}", {"fio": v, "login": v, "is_admin": v, "active": v, "b24_user_id": v}),
                    ("/api/admin/assessments", {"subject_id": v, "is_manager": v, "deadline": v, "respondents": v}),
                    ("/api/admin/assessments", {"subject_id": self.ivanov, "deadline": self.deadline(),
                                                "respondents": [{"user_id": v, "role": v}]}),
                    (f"/api/admin/assessments/{aid}/respondents", {"user_id": v, "role": v}),
                    (f"/api/admin/assessments/{aid}/deadline", {"deadline": v}),
                    (f"/api/assignments/{gid}/answer", {"key": v, "value": v}),
                    (f"/api/assignments/{gid}/answer", {"key": "A1", "value": v}),
                    (f"/api/assignments/{gid}/answer", {"key": "O1", "value": v})):
                code, r = (self.client("petrov") if "/answer" in path else admin).post(path, body)
                self.assertLess(code, 500, (path, body, code))  # None в ответе — удаление, это 200

    def test_xlsx_no_submissions(self):
        admin = self.client("nat")
        aid = self.create_assessment(admin)
        code, headers, data = admin.call("GET", f"/api/admin/assessments/{aid}/xlsx")
        self.assertEqual(code, 200)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))
        self.assertEqual(admin.call("GET", "/api/admin/assessments/9999/xlsx")[0], 404)


class B24UsersTest(ServerTestCase):
    class FakeTasks:
        def __init__(self):
            self.calls, self.error = 0, None

        def users(self):
            self.calls += 1
            if self.error:
                raise self.error
            return [{"id": 5, "name": "Иванов Пётр", "position": "Инженер"}]

    def test_without_webhook_empty(self):
        self.assertEqual(self.client("nat").get("/api/admin/b24-users"), (200, []))

    def test_cache_refresh_and_error(self):
        fake = self.app.tasks = self.FakeTasks()
        admin = self.client("nat")
        self.assertEqual(admin.get("/api/admin/b24-users")[1][0]["id"], 5)
        admin.get("/api/admin/b24-users")
        self.assertEqual(fake.calls, 1)  # второй ответ — из кеша
        admin.get("/api/admin/b24-users?refresh=1")
        self.assertEqual(fake.calls, 2)
        self.app.b24_users_cache = (self.app.b24_users_cache[0] - server.B24_USERS_TTL - 1, [])
        admin.get("/api/admin/b24-users")
        self.assertEqual(fake.calls, 3)  # кеш устарел
        fake.error = B24Error("user.get: нет связи с порталом")
        code, r = admin.get("/api/admin/b24-users?refresh=1")
        self.assertEqual(code, 502)
        self.assertEqual(r["error"], "Битрикс24 недоступен: user.get: нет связи с порталом")

    def test_network_call_is_outside_the_lock(self):
        held = []
        app = self.app

        class Probe(self.FakeTasks):
            def users(self):
                # RLock того же потока всё равно берётся; проверяем из другого потока, что блокировка свободна
                result = []

                def probe():
                    result.append(app.lock.acquire(timeout=2))
                    if result[0]:
                        app.lock.release()

                t = threading.Thread(target=probe)
                t.start()
                t.join()
                held.append(result[0])
                return super().users()

        app.tasks = Probe()
        self.assertEqual(self.client("nat").get("/api/admin/b24-users")[0], 200)
        self.assertEqual(held, [True])


class StaticTest(ServerTestCase):
    def test_real_interface_assets(self):
        c = Client(self.port)
        for path, content_type in (("/", "text/html"), ("/app.js", "application/javascript"),
                                   ("/style.css", "text/css")):
            code, headers, data = c.call("GET", path)
            self.assertEqual(code, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(content_type), path)
            # после обновления приложения браузер не должен брать старый файл из кеша
            self.assertEqual(headers["Cache-Control"], "no-cache", path)
            self.assertGreater(len(data), 100, path)

    def test_static(self):
        with tempfile.TemporaryDirectory() as web:
            with open(os.path.join(web, "index.html"), "w", encoding="utf-8") as f:
                f.write("<!doctype html><title>Оценка 360°</title>")
            with open(os.path.join(web, "app.js"), "w", encoding="utf-8") as f:
                f.write("// js")
            with mock.patch.object(server, "WEB", web):
                c = Client(self.port)
                code, headers, data = c.call("GET", "/")
                self.assertEqual(code, 200)
                self.assertTrue(headers["Content-Type"].startswith("text/html"))
                self.assertEqual(headers["Cache-Control"], "no-cache")
                self.assertEqual(headers["X-Frame-Options"], "SAMEORIGIN")
                self.assertIn("Оценка 360°", data.decode("utf-8"))
                code, headers, _ = c.call("GET", "/app.js")
                self.assertEqual(code, 200)
                self.assertIn("javascript", headers["Content-Type"])
                self.assertEqual(c.call("GET", "/style.css")[0], 404)  # файла в web/ нет
                self.assertEqual(c.call("POST", "/")[0], 404)
                for path in ("/../config.json", "/config.json", "/%2e%2e/config.json", "/web/index.html",
                             "/index.html", "/data/360.sqlite"):
                    self.assertEqual(c.call("GET", path)[0], 404, path)
                # путь с «..» в сыром виде, без нормализации клиентом
                conn = http.client.HTTPConnection("127.0.0.1", self.port)
                try:
                    conn.request("GET", "/../config.json")
                    resp = conn.getresponse()
                    resp.read()
                    self.assertEqual(resp.status, 404)
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
