"""Клиент входящего вебхука Битрикс24 и задачи анкет (только нужные методы).

Права вебхука — только task и user: user.get, tasks.task.add/list/update/complete/renew, task.commentitem.add."""
import datetime as dt
import http.client
import json
import ssl
import time
import urllib.error
import urllib.request


class B24Error(Exception):
    pass


class Bitrix24:
    def __init__(self, cfg):
        self.url = cfg["webhook"].rstrip("/") + "/"
        self.ctx = None if cfg.get("verify_ssl", True) else ssl._create_unverified_context()

    # ---------- транспорт
    def call(self, method, params=None):
        data = json.dumps(params or {}).encode("utf-8")
        try:
            req = urllib.request.Request(self.url + method + ".json", data=data,
                                         headers={"Content-Type": "application/json"})
        except ValueError:  # текст ошибки содержит адрес вебхука — в журнал и карточку его не пускаем
            raise B24Error(f"{method}: неверный адрес вебхука в config.json")
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
                    body = r.read()
                break
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "ignore")
                if "QUERY_LIMIT_EXCEEDED" in text and attempt < 4:
                    time.sleep(2 + attempt * 2)
                    continue
                raise B24Error(f"{method}: HTTP {e.code} {text[:300]}")
            except (OSError, http.client.HTTPException) as e:  # таймаут, обрыв: портал отвечает не с первого раза
                if attempt < 4:
                    time.sleep(5 + attempt * 5)
                    continue
                raise B24Error(f"{method}: нет связи с порталом ({e})")
        try:
            res = json.loads(body.decode("utf-8"))
        except ValueError:
            raise B24Error(f"{method}: ответ портала — не JSON")
        if "error" in res:
            raise B24Error(f"{method}: {res.get('error')} {res.get('error_description', '')}")
        time.sleep(0.4)  # лимит Б24 — около 2 запросов в секунду
        return res

    def list_all(self, method, params, key=None):
        """Постраничная выборка (по 50 записей)."""
        out, start = [], 0
        while True:
            res = self.call(method, {**params, "start": start})
            chunk = res.get("result", [])
            if key:
                chunk = chunk.get(key, [])
            out.extend(chunk)
            if "next" not in res:
                return out
            start = res["next"]


def _id(value, method):
    """ID из ответа портала (строка или число) → int; непонятный ответ — B24Error, чтобы операция повторилась."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise B24Error(f"{method}: непонятный ответ портала")


def _deadline(deadline):
    return f"{deadline}T23:59:00"


class B24Tasks:
    """Задачи оценивающих и список пользователей поверх транспорта Bitrix24."""

    def __init__(self, b24):
        self.b24 = b24

    def users(self):
        """Активные пользователи портала по имени: [{"id", "name": "Фамилия Имя", "position"}]."""
        out = []
        for u in self.b24.list_all("user.get", {"FILTER": {"ACTIVE": True}}):
            if u.get("ACTIVE") in (False, "N"):
                continue
            uid = _id(u.get("ID"), "user.get")
            name = " ".join(p for p in ((u.get("LAST_NAME") or "").strip(), (u.get("NAME") or "").strip()) if p)
            out.append({"id": uid, "name": name or f"Пользователь {uid}",
                        "position": (u.get("WORK_POSITION") or "").strip()})
        out.sort(key=lambda u: (u["name"].casefold(), u["id"]))
        return out

    def find_task(self, responsible_id, title, created_at):
        """Задача анкеты по ответственному и названию. Только созданные не раньше чем за сутки до операции
        create (created_at — её отметка времени): после пересоздания базы номера анкет идут заново, и старая
        закрытая задача с тем же названием не подхватится. Сутки запаса покрывают разницу часовых поясов."""
        since = (dt.datetime.fromisoformat(created_at) - dt.timedelta(days=1)).isoformat(timespec="seconds")
        res = self.b24.call("tasks.task.list", {"filter": {"RESPONSIBLE_ID": responsible_id, "TITLE": title,
                                                           ">=CREATED_DATE": since}, "select": ["ID"]})
        tasks = (res.get("result") or {}).get("tasks") or []
        if not tasks:
            return None
        t = tasks[0]
        return _id(t.get("id", t.get("ID")) if isinstance(t, dict) else None, "tasks.task.list")

    def add_task(self, responsible_id, title, description, deadline):
        res = self.b24.call("tasks.task.add", {"fields": {
            "TITLE": title, "DESCRIPTION": description, "RESPONSIBLE_ID": responsible_id,
            "DEADLINE": _deadline(deadline)}})
        task = (res.get("result") or {}).get("task")
        return _id(task.get("id") if isinstance(task, dict) else None, "tasks.task.add")

    def set_deadline(self, task_id, deadline):
        self.b24.call("tasks.task.update", {"taskId": task_id, "fields": {"DEADLINE": _deadline(deadline)}})

    def complete(self, task_id):
        self.b24.call("tasks.task.complete", {"taskId": task_id})

    def renew(self, task_id):
        self.b24.call("tasks.task.renew", {"taskId": task_id})

    def comment(self, task_id, text):
        self.b24.call("task.commentitem.add", {"TASKID": task_id, "FIELDS": {"POST_MESSAGE": text}})
