import contextlib
import datetime as dt
import io
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import assessments as asm, b24queue, users  # noqa: E402
from app.bitrix24 import B24Error, B24Tasks, Bitrix24  # noqa: E402
from app.common import connect, today  # noqa: E402
from app.questionnaire import score_keys  # noqa: E402

PUBLIC_URL = "https://360.transtehcom.ru:8443"


def day(offset):
    return (today() + dt.timedelta(days=offset)).isoformat()


class FakeTasks:
    """Тот же интерфейс, что у B24Tasks: задачи — в словаре, вызовы — в журнале, сбои — по сценарию.
    Каждый вызов проверяет, что блокировка базы в этот момент свободна; нарушения копятся в violations
    (process перехватывает любые исключения, поэтому одного assert мало)."""

    def __init__(self, lock):
        self.lock = lock
        self.violations = []
        self.tasks = {}
        self.calls = []
        self.fail = {}  # метод → сколько раз подряд упасть
        self.crash = {}  # метод → сколько раз подряд упасть чужой ошибкой (не B24Error)
        self.after_add = None  # вызывается после создания задачи: может удалить анкету или «потерять ответ»
        self.next_id = 500

    def _call(self, method, *args):
        if self.lock is not None and self.lock.locked():
            self.violations.append(method)
            raise AssertionError(f"{method}: вызов Битрикс24 под блокировкой базы")
        self.calls.append((method, *args))
        if self.fail.get(method):
            self.fail[method] -= 1
            raise B24Error(f"{method}: сбой")
        if self.crash.get(method):
            self.crash[method] -= 1
            raise RuntimeError("https://portal.example/rest/1/secret/")

    def find_task(self, responsible_id, title):
        self._call("find_task", responsible_id, title)
        for task_id, t in self.tasks.items():
            if t["responsible_id"] == responsible_id and t["title"] == title:
                return task_id
        return None

    def add_task(self, responsible_id, title, description, deadline):
        self._call("add_task", responsible_id, title, description, deadline)
        self.next_id += 1
        self.tasks[self.next_id] = {"responsible_id": responsible_id, "title": title, "status": "open"}
        if self.after_add:
            self.after_add(self.next_id)
        return self.next_id

    def set_deadline(self, task_id, deadline):
        self._call("set_deadline", task_id, deadline)

    def complete(self, task_id):
        self._call("complete", task_id)
        self.tasks[task_id]["status"] = "closed"

    def renew(self, task_id):
        self._call("renew", task_id)
        self.tasks[task_id]["status"] = "open"

    def comment(self, task_id, text):
        self._call("comment", task_id, text)


class FakeBitrix24:
    """Транспорт для B24Tasks: отдаёт заготовленные ответы и пишет вызовы."""

    def __init__(self, responses=None, users=None):
        self.responses = responses or {}
        self.users = users or []
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        return self.responses.get(method, {"result": True})

    def list_all(self, method, params, key=None):
        self.calls.append((method, params))
        return self.users


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.lock = threading.Lock()
        self.fake = FakeTasks(self.lock)
        self.addCleanup(lambda: self.assertEqual(self.fake.violations, []))
        self.logged = []
        self.nat = users.create_user(self.con, "Наталья", "nat", "secret1", is_admin=True)
        self.ivanov = users.create_user(self.con, "Иванов Пётр", "ivanov", "secret1", b24_user_id=11)
        self.petrov = users.create_user(self.con, "Петров Иван", "petrov", "secret1", b24_user_id=12)
        self.client = users.create_user(self.con, "Клиентов Кирилл", "client", "secret1")
        self.deadline = day(14)
        self.aid = asm.create_assessment(self.con, self.ivanov, False, self.deadline, self.nat,
                                         [(self.petrov, "peer"), (self.client, "client")])
        self.a_peer = self.assignment(self.petrov)

    def assignment(self, user_id):
        return self.con.execute("SELECT id FROM assignments WHERE assessment_id=? AND user_id=?",
                                (self.aid, user_id)).fetchone()["id"]

    def process(self):
        return b24queue.process(self.con, self.fake, PUBLIC_URL, self.lock, log=self.logged.append)

    def task_id(self, assignment_id):
        return self.con.execute("SELECT b24_task_id FROM assignments WHERE id=?",
                                (assignment_id,)).fetchone()["b24_task_id"]

    def op(self, op):
        return self.con.execute("SELECT * FROM b24_ops WHERE op=? ORDER BY id DESC", (op,)).fetchone()

    def methods(self):
        return [c[0] for c in self.fake.calls]

    def submit(self, assignment_id, user_id):
        for k in score_keys(False):
            asm.save_answer(self.con, assignment_id, user_id, k, 4)
        asm.submit(self.con, assignment_id, user_id)

    def test_create_then_complete_flow(self):
        self.assertEqual(self.process(), {"done": 1, "failed": 0})
        task_id = self.task_id(self.a_peer)
        self.assertIsNotNone(task_id)
        title = f"Оценка 360°: Иванов Пётр (анкета №{self.a_peer})"
        d = dt.date.fromisoformat(self.deadline).strftime("%d.%m.%Y")
        self.assertEqual(self.fake.calls, [
            ("find_task", 12, title),
            ("add_task", 12, title,
             f"Пожалуйста, заполните анкету оценки 360° по сотруднику Иванов Пётр до {d}.\n"
             f"Анкета: {PUBLIC_URL}\nВойдите под логином, который выдала Наталья.", self.deadline),
        ])
        self.assertEqual(b24queue.task_status(self.con, self.a_peer), "задача поставлена")
        self.fake.calls.clear()
        self.submit(self.a_peer, self.petrov)
        self.assertEqual(self.process(), {"done": 1, "failed": 0})
        self.assertEqual(self.fake.calls, [("complete", task_id)])
        self.assertEqual(self.process(), {"done": 0, "failed": 0})
        self.assertEqual(self.logged, [])

    def test_no_duplicate_after_lost_response(self):
        def lose_response(task_id):
            self.fake.after_add = None
            raise B24Error("timeout")
        self.fake.after_add = lose_response
        self.assertEqual(self.process(), {"done": 0, "failed": 1})
        self.assertEqual(self.op("create")["attempts"], 1)
        self.assertEqual(self.op("create")["last_error"], "timeout")
        self.assertIsNone(self.task_id(self.a_peer))
        self.assertEqual(len(self.logged), 1)
        self.assertIn("timeout", self.logged[0])
        self.fake.calls.clear()
        self.assertEqual(self.process(), {"done": 1, "failed": 0})
        self.assertEqual(self.methods(), ["find_task"])
        self.assertEqual(len(self.fake.tasks), 1)
        self.assertEqual(self.task_id(self.a_peer), next(iter(self.fake.tasks)))

    def test_ops_wait_for_create(self):
        self.submit(self.a_peer, self.petrov)
        self.fake.fail["add_task"] = 1
        self.assertEqual(self.process(), {"done": 0, "failed": 1})
        self.assertEqual(self.methods(), ["find_task", "add_task"])
        self.assertEqual(self.op("complete")["attempts"], 0)
        self.assertIsNone(self.op("complete")["done_at"])
        self.fake.calls.clear()
        self.assertEqual(self.process(), {"done": 2, "failed": 0})
        task_id = self.task_id(self.a_peer)
        self.assertEqual(self.methods(), ["find_task", "add_task", "complete"])
        self.assertEqual(self.fake.calls[-1], ("complete", task_id))
        self.assertEqual(self.fake.tasks[task_id]["status"], "closed")

    def test_removed_before_create(self):
        asm.remove_respondent(self.con, self.a_peer)
        self.assertEqual(self.process(), {"done": 1, "failed": 0})
        self.assertEqual(self.fake.calls, [])
        self.assertIsNotNone(self.op("create")["done_at"])

    def test_removed_during_create(self):
        self.fake.after_add = lambda task_id: asm.remove_respondent(self.con, self.a_peer)
        self.assertEqual(self.process(), {"done": 2, "failed": 0})  # create и закрытие лишней задачи
        task_id = next(iter(self.fake.tasks))
        self.assertEqual(self.methods(), ["find_task", "add_task", "complete"])
        self.assertEqual(self.fake.calls[-1], ("complete", task_id))
        self.assertIsNotNone(self.op("create")["done_at"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM b24_ops WHERE done_at IS NULL").fetchone()[0], 0)

    def test_removed_during_create_complete_fails(self):
        # задача повисла бы открытой: её закрытие уходит в очередь и повторяется
        self.fake.after_add = lambda task_id: asm.remove_respondent(self.con, self.a_peer)
        self.fake.fail["complete"] = 1
        self.assertEqual(self.process(), {"done": 1, "failed": 1})
        task_id = next(iter(self.fake.tasks))
        self.assertIsNotNone(self.op("create")["done_at"])
        retry = self.op("complete")
        self.assertEqual(json.loads(retry["payload"]), {"task_id": task_id})
        self.assertIsNone(retry["done_at"])
        self.assertEqual(retry["attempts"], 1)
        self.fake.calls.clear()
        self.assertEqual(self.process(), {"done": 1, "failed": 0})
        self.assertEqual(self.fake.calls, [("complete", task_id)])

    def test_removed_with_task_uses_payload(self):
        self.process()
        task_id = self.task_id(self.a_peer)
        self.fake.calls.clear()
        asm.remove_respondent(self.con, self.a_peer)
        self.assertEqual(self.process(), {"done": 2, "failed": 0})
        self.assertEqual(self.fake.calls, [("comment", task_id, "Вы исключены из оценки"), ("complete", task_id)])

    def test_removed_and_readded_gets_new_task(self):
        # номер убранной анкеты не достаётся новой: иначе find_task нашёл бы старую закрытую задачу
        self.process()
        old_task = self.task_id(self.a_peer)
        self.con.execute("UPDATE assignments SET sent_at='2026-10-01T10:00:00' WHERE id=?", (self.a_peer,))
        self.con.commit()
        self.assertEqual(b24queue.due_reminders(self.con, [7, 12], dt.date(2026, 10, 8)), 1)
        asm.remove_respondent(self.con, self.assignment(self.client))
        asm.remove_respondent(self.con, self.a_peer)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM reminders WHERE assignment_id=?",
                                          (self.a_peer,)).fetchone()[0], 0)
        again = asm.add_respondent(self.con, self.aid, self.petrov, "peer")
        self.assertNotEqual(again, self.a_peer)
        self.process()
        new_task = self.task_id(again)
        self.assertEqual(self.methods().count("add_task"), 2)
        self.assertNotEqual(new_task, old_task)
        self.assertEqual(self.fake.tasks[old_task]["status"], "closed")
        self.assertEqual(self.fake.tasks[new_task]["status"], "open")
        self.con.execute("UPDATE assignments SET sent_at='2026-10-01T10:00:00' WHERE id=?", (again,))
        self.con.commit()
        self.assertEqual(b24queue.due_reminders(self.con, [7, 12], dt.date(2026, 10, 8)), 1)

    def test_unexpected_error_counts_attempt(self):
        sidorov = users.create_user(self.con, "Сидоров Олег", "sidorov", "secret1", b24_user_id=13)
        other = asm.add_respondent(self.con, self.aid, sidorov, "peer")
        self.fake.crash["find_task"] = 1
        self.assertEqual(self.process(), {"done": 1, "failed": 1})
        create = self.con.execute("SELECT * FROM b24_ops WHERE assignment_id=? AND op='create'",
                                  (self.a_peer,)).fetchone()
        self.assertEqual((create["attempts"], create["last_error"]), (1, "внутренняя ошибка: RuntimeError"))
        self.assertIsNotNone(self.task_id(other))  # очередь других анкет не встала
        self.assertEqual(len(self.logged), 1)
        self.assertNotIn("secret", self.logged[0])
        self.assertEqual(self.process(), {"done": 1, "failed": 0})
        self.assertIsNotNone(self.task_id(self.a_peer))

    def test_attempt_cap_and_retry(self):
        self.submit(self.a_peer, self.petrov)
        self.fake.fail["add_task"] = b24queue.MAX_ATTEMPTS
        for _ in range(b24queue.MAX_ATTEMPTS):
            self.assertEqual(self.process(), {"done": 0, "failed": 1})
        self.assertEqual(self.op("create")["attempts"], b24queue.MAX_ATTEMPTS)
        self.fake.calls.clear()
        self.assertEqual(self.process(), {"done": 0, "failed": 0})
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.op("complete")["attempts"], 0)
        self.assertTrue(b24queue.task_status(self.con, self.a_peer).startswith("задача не поставлена"))
        b24queue.retry(self.con, self.a_peer)
        self.assertEqual(self.op("create")["attempts"], 0)
        self.assertIsNone(self.op("create")["last_error"])
        self.assertEqual(self.process(), {"done": 2, "failed": 0})
        self.assertEqual(self.methods(), ["find_task", "add_task", "complete"])
        self.assertEqual(b24queue.task_status(self.con, self.a_peer), "задача поставлена")

    def test_failed_op_blocks_later_ops_of_same_assignment(self):
        self.process()
        task_id = self.task_id(self.a_peer)
        self.fake.calls.clear()
        asm.cancel_assessment(self.con, self.aid)
        self.fake.fail["comment"] = 1
        self.assertEqual(self.process(), {"done": 0, "failed": 1})
        self.assertEqual(self.fake.calls, [("comment", task_id, "Оценка отменена")])
        self.assertTrue(b24queue.task_status(self.con, self.a_peer).startswith("ошибка Битрикс24"))

    def test_reminders_days_7_and_12(self):
        self.process()
        self.con.execute("UPDATE assignments SET sent_at='2026-10-01T10:00:00' WHERE id=?", (self.a_peer,))
        self.con.commit()

        def due(d):
            return b24queue.due_reminders(self.con, [7, 12], dt.date.fromisoformat(d))
        self.assertEqual(due("2026-10-07"), 0)
        self.assertEqual(due("2026-10-08"), 1)
        self.assertEqual(due("2026-10-13"), 1)
        self.assertEqual(due("2026-10-13"), 0)
        d = dt.date.fromisoformat(self.deadline).strftime("%d.%m")
        comments = [json.loads(r["payload"]) for r in self.con.execute(
            "SELECT payload FROM b24_ops WHERE op='comment' AND assignment_id=? ORDER BY id", (self.a_peer,))]
        self.assertEqual(comments, [{"text": f"Напоминание: анкета по Иванов Пётр ещё не заполнена, срок — {d}"}] * 2)
        self.assertEqual(self.con.execute("SELECT day FROM reminders ORDER BY day").fetchall()[1][0], 12)

        # отправленной анкете не напоминают
        self.submit(self.a_peer, self.petrov)
        self.con.execute("DELETE FROM reminders")
        self.con.commit()
        self.assertEqual(due("2026-10-20"), 0)

        # анкете без задачи не напоминают; добавленной позже — отсчёт от её добавления
        sidorov = users.create_user(self.con, "Сидоров Олег", "sidorov", "secret1", b24_user_id=13)
        late = asm.add_respondent(self.con, self.aid, sidorov, "peer")
        self.con.execute("UPDATE assignments SET sent_at='2026-10-05T09:00:00' WHERE id=?", (late,))
        self.con.commit()
        self.assertEqual(due("2026-10-20"), 0)
        self.process()
        # сервер был выключен с 10-05 до 10-20: оба напоминания — по одному разу
        before = self.con.execute("SELECT COUNT(*) FROM b24_ops WHERE op='comment'").fetchone()[0]
        self.assertEqual(due("2026-10-20"), 2)
        self.assertEqual(due("2026-10-21"), 0)
        rows = self.con.execute("SELECT assignment_id, payload FROM b24_ops WHERE op='comment' ORDER BY id"
                                ).fetchall()[before:]
        self.assertEqual([r["assignment_id"] for r in rows], [late, late])
        self.assertEqual(self.con.execute("SELECT day FROM reminders WHERE assignment_id=? ORDER BY day",
                                          (late,)).fetchall()[0][0], 7)

    def test_reminders_skip_closed_assessment(self):
        self.process()
        self.con.execute("UPDATE assignments SET sent_at='2026-10-01T10:00:00'")
        self.con.commit()
        asm.cancel_assessment(self.con, self.aid)
        self.assertEqual(b24queue.due_reminders(self.con, [7, 12], dt.date(2026, 10, 20)), 0)

    def test_return_reopens_same_task(self):
        self.process()
        task_id = self.task_id(self.a_peer)
        self.submit(self.a_peer, self.petrov)
        self.process()
        self.fake.calls.clear()
        asm.return_for_revision(self.con, self.a_peer)
        self.assertEqual(self.process(), {"done": 2, "failed": 0})
        self.assertEqual(self.fake.calls, [("renew", task_id), ("comment", task_id, "Анкета возвращена на доработку")])
        self.assertEqual(len(self.fake.tasks), 1)
        self.assertEqual(self.fake.tasks[task_id]["status"], "open")

    def test_cancel_comments_then_completes(self):
        self.process()
        task_id = self.task_id(self.a_peer)
        self.fake.calls.clear()
        asm.cancel_assessment(self.con, self.aid)
        self.assertEqual(self.process(), {"done": 2, "failed": 0})
        self.assertEqual(self.fake.calls, [("comment", task_id, "Оценка отменена"), ("complete", task_id)])

    def test_deadline_change(self):
        self.process()
        task_id = self.task_id(self.a_peer)
        self.fake.calls.clear()
        asm.set_deadline(self.con, self.aid, day(20))
        self.process()
        self.assertEqual(self.fake.calls, [("set_deadline", task_id, day(20))])

    def test_create_takes_current_data(self):
        # срок сменили до того, как задача поставилась: задача ставится сразу с новым сроком
        asm.set_deadline(self.con, self.aid, day(30))
        self.process()
        self.assertEqual(self.fake.calls[1][4], day(30))
        self.assertEqual(self.methods(), ["find_task", "add_task", "set_deadline"])

    def test_worker_runs_pass_on_start_and_wake(self):
        config = {"server": {"public_url": PUBLIC_URL}, "reminder_days": [7, 12],
                  "bitrix24": {"webhook": "https://example.invalid/rest/1/x/"}}
        # здесь блокировку держит и тестовый поток, поэтому проверку «вызов не под блокировкой» отключаем
        self.fake.lock = None
        worker = b24queue.Worker(self.con, self.lock, config, self.fake, log=self.logged.append)
        self.addCleanup(worker.stop)
        self.assertTrue(worker.start())
        self.wait_for(lambda: self.task_id(self.a_peer) is not None)
        task_id = self.task_id(self.a_peer)
        with self.lock:
            self.submit(self.a_peer, self.petrov)
        worker.wake()
        self.wait_for(lambda: ("complete", task_id) in self.fake.calls)
        worker.stop()
        self.assertFalse(worker.thread.is_alive())

    def test_worker_survives_exception(self):
        config = {"server": {"public_url": PUBLIC_URL}, "reminder_days": [7, 12],
                  "bitrix24": {"webhook": "https://example.invalid/rest/1/x/"}}
        self.fake.lock = None
        worker = b24queue.Worker(self.con, self.lock, config, self.fake, log=self.logged.append)
        self.addCleanup(worker.stop)
        run_once, passes = worker.run_once, []

        def flaky():
            passes.append(1)
            if len(passes) == 1:
                raise RuntimeError("сбой прохода")
            run_once()
        worker.run_once = flaky
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            worker.start()
            self.wait_for(lambda: passes)
            self.assertIsNone(self.task_id(self.a_peer))
            worker.wake()
            self.wait_for(lambda: self.task_id(self.a_peer) is not None)
            worker.stop()
        self.assertIn("RuntimeError: сбой прохода", stderr.getvalue())
        self.assertFalse(worker.thread.is_alive())

    def test_worker_not_started_without_webhook(self):
        config = {"server": {"public_url": PUBLIC_URL}, "reminder_days": [7, 12], "bitrix24": {"webhook": ""}}
        worker = b24queue.Worker(self.con, self.lock, config, self.fake)
        self.assertFalse(worker.start())
        self.assertIsNone(worker.thread)

    def wait_for(self, condition, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if condition():
                    return
            time.sleep(0.01)
        self.fail("фоновый поток не выполнил проход")


class B24TasksTest(unittest.TestCase):
    def test_users_filters_inactive(self):
        b24 = FakeBitrix24(users=[
            {"ID": "7", "LAST_NAME": "Петров", "NAME": "Иван", "WORK_POSITION": "Инженер", "ACTIVE": True},
            {"ID": "5", "LAST_NAME": "Уволенный", "NAME": "Олег", "WORK_POSITION": "", "ACTIVE": False},
            {"ID": "3", "LAST_NAME": "Акулова", "NAME": "Анна", "WORK_POSITION": None, "ACTIVE": True},
            {"ID": "9", "LAST_NAME": "", "NAME": "Сергей ", "ACTIVE": True},
        ])
        self.assertEqual(B24Tasks(b24).users(), [
            {"id": 3, "name": "Акулова Анна", "position": ""},
            {"id": 7, "name": "Петров Иван", "position": "Инженер"},
            {"id": 9, "name": "Сергей", "position": ""},
        ])
        self.assertEqual(b24.calls, [("user.get", {"FILTER": {"ACTIVE": True}})])

    def test_task_methods(self):
        b24 = FakeBitrix24(responses={
            "tasks.task.list": {"result": {"tasks": [{"id": "42"}]}},
            "tasks.task.add": {"result": {"task": {"id": "43"}}},
        })
        t = B24Tasks(b24)
        self.assertEqual(t.find_task(12, "Оценка 360°: Иванов Пётр (анкета №1)"), 42)
        self.assertEqual(t.add_task(12, "Заголовок", "Описание", "2026-11-09"), 43)
        t.set_deadline(43, "2026-11-20")
        t.complete(43)
        t.renew(43)
        t.comment(43, "Текст")
        self.assertEqual(b24.calls, [
            ("tasks.task.list", {"filter": {"RESPONSIBLE_ID": 12, "TITLE": "Оценка 360°: Иванов Пётр (анкета №1)"},
                                 "select": ["ID"]}),
            ("tasks.task.add", {"fields": {"TITLE": "Заголовок", "DESCRIPTION": "Описание", "RESPONSIBLE_ID": 12,
                                           "DEADLINE": "2026-11-09T23:59:00"}}),
            ("tasks.task.update", {"taskId": 43, "fields": {"DEADLINE": "2026-11-20T23:59:00"}}),
            ("tasks.task.complete", {"taskId": 43}),
            ("tasks.task.renew", {"taskId": 43}),
            ("task.commentitem.add", {"TASKID": 43, "FIELDS": {"POST_MESSAGE": "Текст"}}),
        ])

    def test_bad_webhook_hidden_in_error(self):
        with self.assertRaises(B24Error) as cm:
            Bitrix24({"webhook": "no-scheme/abc"}).call("profile")
        self.assertIn("profile", str(cm.exception))
        self.assertNotIn("no-scheme", str(cm.exception))

    def test_find_task_none_and_bad_answer(self):
        t = B24Tasks(FakeBitrix24(responses={"tasks.task.list": {"result": {"tasks": []}},
                                             "tasks.task.add": {"result": {"task": None}}}))
        self.assertIsNone(t.find_task(12, "нет такой"))
        with self.assertRaises(B24Error):
            t.add_task(12, "Заголовок", "Описание", "2026-11-09")


if __name__ == "__main__":
    unittest.main()
