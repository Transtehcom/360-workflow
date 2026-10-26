"""Очередь операций Битрикс24: постановка в очередь, выполнение, напоминания, фоновый поток.

Операции пишутся в b24_ops в той же транзакции, что и изменение анкеты. Выполняет их process:
по каждой анкете строго по порядку, вызовы Битрикс24 — вне блокировки базы."""
import datetime as dt
import json
import threading
import time
import traceback

from .bitrix24 import B24Error
from .common import db_lock, now, today

OPS = ("create", "deadline", "comment", "complete", "renew")
MAX_ATTEMPTS = 24  # после стольких неудачных попыток операция больше не повторяется
PERIOD = 3600  # секунд: раз в столько фоновый поток проверяет напоминания и проходит по очереди


def enqueue(con, assignment_id, op, payload=None):
    """Ставит операцию в очередь, возвращает её id. Не делает commit: запись уходит вместе с изменением
    вызывающего."""
    if op not in OPS:
        raise ValueError(f"Неизвестная операция Битрикс24: {op}")
    return con.execute("INSERT INTO b24_ops (assignment_id, op, payload, created_at) VALUES (?,?,?,?)",
                       (assignment_id, op, json.dumps(payload or {}, ensure_ascii=False), now())).lastrowid


def has_task(con, assignment_id):
    """Есть ли у анкеты задача: уже поставлена или ещё ставится (create не выполнен и не исчерпал попытки).
    Только тогда есть смысл ставить в очередь остальные операции — им есть чего дождаться."""
    return con.execute(
        "SELECT 1 FROM assignments WHERE id=? AND b24_task_id IS NOT NULL UNION ALL "
        "SELECT 1 FROM b24_ops WHERE assignment_id=? AND op='create' AND done_at IS NULL AND attempts < ?",
        (assignment_id, assignment_id, MAX_ATTEMPTS)).fetchone() is not None


def task_status(con, assignment_id):
    """Подпись для карточки оценки: что с задачей оценивающего в Битрикс24."""
    a = con.execute("SELECT a.b24_task_id, u.b24_user_id FROM assignments a JOIN users u ON u.id = a.user_id "
                    "WHERE a.id=?", (assignment_id,)).fetchone()
    if not a or a["b24_user_id"] is None:
        return "не связан с Битрикс24"
    pending = con.execute("SELECT op, attempts, last_error FROM b24_ops WHERE assignment_id=? AND done_at IS NULL "
                          "ORDER BY id", (assignment_id,)).fetchall()
    exhausted = [p for p in pending if p["attempts"] >= MAX_ATTEMPTS]
    if any(p["op"] == "create" for p in exhausted):
        return "задача не поставлена"
    if exhausted and a["b24_task_id"] is not None:
        return "задача не обновилась в Битрикс24"
    failed = [p for p in pending if p["last_error"] and p["attempts"] < MAX_ATTEMPTS]
    if failed:
        return f"ошибка Битрикс24: {failed[0]['last_error']}"
    if any(p["op"] == "create" for p in pending):
        return "ставится…"
    if a["b24_task_id"] is not None:
        return "задача поставлена"
    return "задача не поставлена"


def task_title(subject_fio, assignment_id):
    """Название задачи. Номер анкеты делает его уникальным: по нему задача находится при повторе."""
    return f"Оценка 360°: {subject_fio} (анкета №{assignment_id})"


def task_description(subject_fio, deadline, public_url):
    d = dt.date.fromisoformat(deadline).strftime("%d.%m.%Y")
    return (f"Пожалуйста, заполните анкету оценки 360° по сотруднику {subject_fio} до {d}.\n"
            f"Анкета: {public_url}\nВойдите под логином, который выдала Наталья.")


def due_reminders(con, days, today):
    """Ставит в очередь напоминания анкетам в работе, у которых есть задача: на d-й день от выдачи анкеты.
    Каждое напоминание — один раз (таблица reminders), пропущенные дни — тоже по разу. Делает commit;
    вызывать под блокировкой базы. Возвращает число напоминаний."""
    rows = con.execute(
        "SELECT g.id, g.sent_at, a.deadline, s.fio AS subject_fio FROM assignments g "
        "JOIN assessments a ON a.id = g.assessment_id JOIN users s ON s.id = a.subject_id "
        "WHERE g.status='draft' AND a.status='active' AND g.b24_task_id IS NOT NULL ORDER BY g.id").fetchall()
    count = 0
    try:
        for g in rows:
            sent = dt.date.fromisoformat(g["sent_at"][:10])
            for d in sorted(set(days)):
                if today < sent + dt.timedelta(days=d):
                    continue
                if con.execute("INSERT OR IGNORE INTO reminders (assignment_id, day, sent_at) VALUES (?,?,?)",
                               (g["id"], d, now())).rowcount:
                    deadline = dt.date.fromisoformat(g["deadline"]).strftime("%d.%m")
                    enqueue(con, g["id"], "comment", {
                        "text": f"Напоминание: анкета по {g['subject_fio']} ещё не заполнена, срок — {deadline}"})
                    count += 1
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return count


def _finish(con, op_id):
    con.execute("UPDATE b24_ops SET done_at=? WHERE id=?", (now(), op_id))


def _create(con, tasks, public_url, lock, op):
    """Ставит задачу анкеты. Ответственный, оцениваемый и срок — из базы на момент выполнения.
    Если найденную или поставленную задачу некому закрыть (анкету убрали или уже отправили), возвращает
    операцию её закрытия, иначе None."""
    aid = op["assignment_id"]
    with lock:
        g = con.execute(
            "SELECT g.b24_task_id, g.status, a.status AS assessment_status, u.b24_user_id, s.fio AS subject_fio, "
            "a.deadline FROM assignments g JOIN users u ON u.id = g.user_id "
            "JOIN assessments a ON a.id = g.assessment_id JOIN users s ON s.id = a.subject_id WHERE g.id=?",
            (aid,)).fetchone()
        skip = (not g or g["b24_task_id"] is not None or g["b24_user_id"] is None
                or g["status"] != "draft" or g["assessment_status"] != "active")
        # анкету отправили или оценку закрыли после неудачной попытки: задача могла поставиться с потерянным
        # ответом. Новую не ставим, но ищем — найденную надо закрыть
        adopt = (skip and g is not None and g["b24_task_id"] is None and g["b24_user_id"] is not None
                 and op["attempts"] > 0)
        if skip and not adopt:
            # анкету убрали, задача уже есть, связь с Битрикс24 сняли, анкету отправили или оценку закрыли —
            # ставить нечего. Отправка и закрытие без задачи не ставят complete в очередь: поставленную сейчас
            # задачу никто бы не закрыл. Операции за этой завершатся в _task_op как «задачи нет»
            _finish(con, op["id"])
            con.commit()
            return None
    title = task_title(g["subject_fio"], aid)
    # задача могла поставиться, а ответ — потеряться: сначала ищем её, чтобы не завести вторую
    task_id = tasks.find_task(g["b24_user_id"], title, op["created_at"])
    if task_id is None:
        if adopt:
            with lock:
                _finish(con, op["id"])  # задачи нет — ставить её отправленной или закрытой анкете незачем
                con.commit()
            return None
        task_id = tasks.add_task(g["b24_user_id"], title,
                                 task_description(g["subject_fio"], g["deadline"], public_url), g["deadline"])
    with lock:
        # ID задачи и выполнение create — одной транзакцией: на это опирается has_task
        stored = con.execute("UPDATE assignments SET b24_task_id=? WHERE id=?", (task_id, aid)).rowcount
        _finish(con, op["id"])
        follow_up = None
        # закрытие, поставленное отправкой или закрытием оценки, уже ждёт в очереди и теперь найдёт задачу
        waiting = adopt and con.execute("SELECT 1 FROM b24_ops WHERE assignment_id=? AND op='complete' "
                                        "AND done_at IS NULL", (aid,)).fetchone() is not None
        if not stored or (adopt and not waiting):
            # анкету убрали, пока ставилась задача, или закрыть найденную некому: закрытие — в очередь той же
            # транзакцией, выполняется сразу
            follow_up = enqueue(con, aid, "complete", {"task_id": task_id})
        con.commit()
        if follow_up is None:
            return None
        return con.execute("SELECT * FROM b24_ops WHERE id=?", (follow_up,)).fetchone()


def _task_op(con, tasks, lock, op):
    """Операция над задачей. False — задачи ещё нет, а create не выполнен: операция ждёт."""
    payload = json.loads(op["payload"])
    with lock:
        task_id = payload.get("task_id")
        if task_id is None:
            row = con.execute("SELECT b24_task_id FROM assignments WHERE id=?", (op["assignment_id"],)).fetchone()
            task_id = row["b24_task_id"] if row else None
        if task_id is None:
            if con.execute("SELECT 1 FROM b24_ops WHERE assignment_id=? AND op='create' AND done_at IS NULL",
                           (op["assignment_id"],)).fetchone():
                return False
            _finish(con, op["id"])  # задачи нет и не будет — делать нечего
            con.commit()
            return True
    if op["op"] == "deadline":
        tasks.set_deadline(task_id, payload["deadline"])
    elif op["op"] == "comment":
        tasks.comment(task_id, payload["text"])
    elif op["op"] == "complete":
        tasks.complete(task_id)
    elif op["op"] == "renew":
        tasks.renew(task_id)
    with lock:
        _finish(con, op["id"])
        con.commit()
    return True


def process(con, tasks, public_url, lock, log=print):
    """Проход по очереди. По каждой анкете операции идут строго по порядку: не выполнилась одна —
    следующие ждут. Сбой Битрикс24 — попытка засчитывается, операция остаётся в очереди."""
    with lock:
        ops = con.execute("SELECT * FROM b24_ops WHERE done_at IS NULL AND attempts < ? ORDER BY id",
                          (MAX_ATTEMPTS,)).fetchall()
    result = {"done": 0, "failed": 0}
    stalled = set()  # анкеты, у которых очередь в этом проходе остановилась

    def run(op):
        try:
            if op["op"] == "create":
                follow_up = _create(con, tasks, public_url, lock, op)
            else:
                follow_up = None
                if not _task_op(con, tasks, lock, op):
                    stalled.add(op["assignment_id"])  # ждёт задачу; попыткой не считается
                    return
        except Exception as e:
            # чужая ошибка (не B24Error) — тоже попытка: иначе операция заслонила бы очередь навсегда.
            # Её текст может содержать адрес вебхука, поэтому пишется только тип
            error = str(e)[:300] if isinstance(e, B24Error) else f"внутренняя ошибка: {type(e).__name__}"
            with lock:
                con.rollback()
                con.execute("UPDATE b24_ops SET attempts=attempts+1, last_error=? WHERE id=?", (error, op["id"]))
                con.commit()
            log(f"Битрикс24: {op['op']} по анкете №{op['assignment_id']} не выполнена — {error}")
            result["failed"] += 1
            stalled.add(op["assignment_id"])
            return
        result["done"] += 1
        if follow_up is not None:
            run(follow_up)

    for op in ops:
        if op["assignment_id"] not in stalled:
            run(op)
    return result


def retry(con, assignment_id):
    """Кнопка «Повторить»: невыполненные операции анкеты снова берутся в работу с нуля попыток."""
    with db_lock():
        con.execute("UPDATE b24_ops SET attempts=0, last_error=NULL WHERE assignment_id=? AND done_at IS NULL",
                    (assignment_id,))
        con.commit()


class Worker:
    """Фоновый поток: раз в час — напоминания, затем проход по очереди; wake() — внеочередной проход."""

    def __init__(self, con, lock, config, tasks, log=print):
        self.con, self.lock, self.tasks, self.log = con, lock, tasks, log
        self.public_url = config["server"]["public_url"]
        self.reminder_days = config["reminder_days"]
        self.enabled = bool((config.get("bitrix24") or {}).get("webhook"))
        self.event = threading.Event()
        self.stopping = False
        self.thread = None
        self.next_reminders = None

    def start(self):
        """Запускает поток. Без адреса вебхука не запускается: задачи в Битрикс24 не ставятся."""
        if not self.enabled:
            return False
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="b24-worker", daemon=True)
            self.thread.start()
        return True

    def wake(self):
        """Немедленный проход — после действий в интерфейсе."""
        self.event.set()

    def stop(self, timeout=5):
        self.stopping = True
        self.event.set()
        if self.thread is not None:
            self.thread.join(timeout)

    def run_once(self):
        if self.next_reminders is None or time.monotonic() >= self.next_reminders:
            self.next_reminders = time.monotonic() + PERIOD
            try:
                with self.lock:
                    due_reminders(self.con, self.reminder_days, today())
            except Exception:  # сбой напоминаний не должен останавливать очередь
                traceback.print_exc()
        process(self.con, self.tasks, self.public_url, self.lock, self.log)

    def _loop(self):
        while not self.stopping:
            try:
                self.run_once()
            except Exception:
                traceback.print_exc()
            self.event.wait(PERIOD)
            self.event.clear()
