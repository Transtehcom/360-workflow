"""Оценки и анкеты: назначение, автосохранение ответов, отправка, возврат, завершение.

Операции с задачами Битрикс24 ставятся в очередь (b24queue) в той же транзакции, что и изменение анкеты:
create — только для оценивающих со связью с Битрикс24, остальные — только если у анкеты есть задача
(b24queue.has_task)."""
import contextlib
import datetime as dt
import re

from . import b24queue
from .common import BadRequest, Conflict, NotFound, db_lock, now, today
from .questionnaire import ROLES, blocks_for, check_answer

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ROLE_ORDER = {role: i for i, role in enumerate(ROLES)}

NOT_EDITABLE = "Анкета уже отправлена или оценка закрыта"
NOT_ACTIVE = "Оценка уже завершена или отменена"


@contextlib.contextmanager
def _write(con):
    """Запись под общей блокировкой: commit в конце, откат при любой ошибке."""
    with db_lock():
        try:
            yield
            con.commit()
        except BaseException:
            con.rollback()
            raise


def _deadline(value):
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise BadRequest("Срок — дата в формате ГГГГ-ММ-ДД")
    try:
        d = dt.date.fromisoformat(value)
    except ValueError:
        raise BadRequest("Срок — дата в формате ГГГГ-ММ-ДД")
    if d < today():
        raise BadRequest("Срок не может быть раньше сегодняшнего дня")
    return value


def _flag(value):
    if isinstance(value, bool) or value in (0, 1):
        return 1 if value else 0
    raise BadRequest("Поле «Оценивается как руководитель» должно быть да или нет")


def _active_user(con, user_id):
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        return None
    return con.execute("SELECT id, fio FROM users WHERE id=? AND active=1", (user_id,)).fetchone()


def _check_respondent(con, subject_id, user_id, role):
    if not isinstance(role, str) or role not in ROLES:
        raise BadRequest("Неизвестная роль оценивающего")
    if not _active_user(con, user_id):
        raise BadRequest("Оценивающий не найден или отключён")
    if (role == "self") != (user_id == subject_id):
        raise BadRequest("Роль «самооценка» — только у оцениваемого, и у него может быть только она")


def _linked(con, user_id):
    r = con.execute("SELECT b24_user_id FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(r and r["b24_user_id"] is not None)


def _insert_assignment(con, assessment_id, user_id, role):
    cur = con.execute("INSERT INTO assignments (assessment_id, user_id, role, sent_at) VALUES (?,?,?,?)",
                      (assessment_id, user_id, role, now()))
    if _linked(con, user_id):
        b24queue.enqueue(con, cur.lastrowid, "create")
    return cur.lastrowid


def _active_assessment(con, assessment_id):
    a = con.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not a:
        raise NotFound("Оценка не найдена")
    if a["status"] != "active":
        raise Conflict(NOT_ACTIVE)
    return a


def _assignment(con, assignment_id):
    """Анкета вместе с оценкой и оцениваемым."""
    return con.execute(
        "SELECT g.*, a.is_manager, a.deadline, a.status AS assessment_status, s.fio AS subject_fio "
        "FROM assignments g JOIN assessments a ON a.id = g.assessment_id "
        "JOIN users s ON s.id = a.subject_id WHERE g.id=?",
        (assignment_id,)).fetchone()


def _editable(g):
    return g["status"] == "draft" and g["assessment_status"] == "active"


def _own_editable(con, assignment_id, user_id):
    g = _assignment(con, assignment_id)
    if not g or g["user_id"] != user_id:
        raise NotFound("Анкета не найдена")
    if not _editable(g):
        raise Conflict(NOT_EDITABLE)
    return g


def _answers(con, assignment_id):
    return {r["key"]: r["value"] for r in con.execute(
        "SELECT key, value FROM answers WHERE assignment_id=?", (assignment_id,))}


def _missing_blocks(answers, is_manager):
    """Блоки, в которых оценены не все утверждения, — по порядку анкеты."""
    return [b["key"] for b in blocks_for(is_manager) if any(i["key"] not in answers for i in b["items"])]


def create_assessment(con, subject_id, is_manager, deadline, created_by, respondents):
    is_manager, deadline = _flag(is_manager), _deadline(deadline)
    with _write(con):
        if not _active_user(con, subject_id):
            raise BadRequest("Оцениваемый не найден или отключён")
        if not isinstance(respondents, (list, tuple)) or not respondents:
            raise BadRequest("Добавьте хотя бы одного оценивающего")
        if any(not isinstance(r, (list, tuple)) or len(r) != 2 for r in respondents):
            raise BadRequest("Оценивающий — пара «участник, роль»")
        seen = set()
        for user_id, role in respondents:
            _check_respondent(con, subject_id, user_id, role)
            if user_id in seen:
                raise BadRequest("Участник встречается в оценке дважды")
            seen.add(user_id)
        cur = con.execute("INSERT INTO assessments (subject_id, is_manager, deadline, created_by, created_at) "
                          "VALUES (?,?,?,?,?)", (subject_id, is_manager, deadline, created_by, now()))
        for user_id, role in respondents:
            _insert_assignment(con, cur.lastrowid, user_id, role)
        return cur.lastrowid


_LIST_SQL = (
    "SELECT a.id, s.fio AS subject_fio, a.deadline, a.status, a.is_manager, "
    "(SELECT COUNT(*) FROM assignments g WHERE g.assessment_id = a.id AND g.status = 'submitted') AS submitted, "
    "(SELECT COUNT(*) FROM assignments g WHERE g.assessment_id = a.id) AS total "
    "FROM assessments a JOIN users s ON s.id = a.subject_id")


def _summary(r):
    return {"id": r["id"], "subject_fio": r["subject_fio"], "deadline": r["deadline"], "status": r["status"],
            "submitted": r["submitted"], "total": r["total"]}


def list_assessments(con):
    return [_summary(r) for r in con.execute(_LIST_SQL + " ORDER BY a.id DESC")]


def get_assessment(con, assessment_id):
    r = con.execute(_LIST_SQL + " WHERE a.id=?", (assessment_id,)).fetchone()
    if not r:
        raise NotFound("Оценка не найдена")
    result = _summary(r)
    result["is_manager"] = bool(r["is_manager"])
    rows = con.execute("SELECT g.*, u.fio FROM assignments g JOIN users u ON u.id = g.user_id "
                       "WHERE g.assessment_id=?", (assessment_id,)).fetchall()
    rows.sort(key=lambda g: (ROLE_ORDER[g["role"]], g["fio"].casefold(), g["id"]))
    result["respondents"] = [{
        "assignment_id": g["id"], "user_id": g["user_id"], "fio": g["fio"], "role": g["role"],
        "role_title": ROLES[g["role"]], "status": g["status"], "submitted_at": g["submitted_at"],
        "returned_at": g["returned_at"], "task_status": b24queue.task_status(con, g["id"]),
    } for g in rows]
    return result


def add_respondent(con, assessment_id, user_id, role):
    with _write(con):
        a = _active_assessment(con, assessment_id)
        _check_respondent(con, a["subject_id"], user_id, role)
        if con.execute("SELECT 1 FROM assignments WHERE assessment_id=? AND user_id=?",
                       (assessment_id, user_id)).fetchone():
            raise BadRequest("Участник уже есть в этой оценке")
        return _insert_assignment(con, assessment_id, user_id, role)


def remove_respondent(con, assignment_id):
    with _write(con):
        g = _assignment(con, assignment_id)
        if not g:
            raise NotFound("Анкета не найдена")
        if g["assessment_status"] != "active":
            raise Conflict(NOT_ACTIVE)
        if g["status"] == "submitted":
            raise Conflict("Анкета уже отправлена — её нельзя убрать")
        con.execute("DELETE FROM answers WHERE assignment_id=?", (assignment_id,))
        con.execute("DELETE FROM reminders WHERE assignment_id=?", (assignment_id,))
        con.execute("DELETE FROM assignments WHERE id=?", (assignment_id,))
        # строки анкеты больше нет, поэтому ID задачи едет в payload;
        # если задачи ещё нет, process пропустит create удалённой анкеты
        task_id = g["b24_task_id"]
        if task_id is not None:
            b24queue.enqueue(con, assignment_id, "comment", {"text": "Вы исключены из оценки", "task_id": task_id})
            b24queue.enqueue(con, assignment_id, "complete", {"task_id": task_id})


def _drafts_with_task(con, assessment_id):
    ids = [r["id"] for r in con.execute(
        "SELECT id FROM assignments WHERE assessment_id=? AND status='draft' ORDER BY id", (assessment_id,))]
    return [g for g in ids if b24queue.has_task(con, g)]


def set_deadline(con, assessment_id, deadline):
    deadline = _deadline(deadline)
    with _write(con):
        _active_assessment(con, assessment_id)
        con.execute("UPDATE assessments SET deadline=? WHERE id=?", (deadline, assessment_id))
        for g in _drafts_with_task(con, assessment_id):
            b24queue.enqueue(con, g, "deadline", {"deadline": deadline})


def _close(con, assessment_id, status, text):
    with _write(con):
        _active_assessment(con, assessment_id)
        drafts = _drafts_with_task(con, assessment_id)
        con.execute("UPDATE assessments SET status=?, closed_at=? WHERE id=?", (status, now(), assessment_id))
        con.execute("UPDATE assignments SET status='closed' WHERE assessment_id=? AND status='draft'",
                    (assessment_id,))
        for g in drafts:
            b24queue.enqueue(con, g, "comment", {"text": text})
            b24queue.enqueue(con, g, "complete")


def finish_assessment(con, assessment_id):
    _close(con, assessment_id, "finished", "Оценка завершена")


def cancel_assessment(con, assessment_id):
    _close(con, assessment_id, "cancelled", "Оценка отменена")


def my_assignments(con, user_id):
    rows = con.execute(
        "SELECT g.id, g.role, a.deadline, a.is_manager, s.fio AS subject_fio FROM assignments g "
        "JOIN assessments a ON a.id = g.assessment_id JOIN users s ON s.id = a.subject_id "
        "WHERE g.user_id=? AND g.status='draft' AND a.status='active'", (user_id,)).fetchall()
    result = []
    for g in rows:
        blocks = blocks_for(bool(g["is_manager"]))
        missing = _missing_blocks(_answers(con, g["id"]), bool(g["is_manager"]))
        result.append({"assignment_id": g["id"], "subject_fio": g["subject_fio"], "role_title": ROLES[g["role"]],
                       "deadline": g["deadline"], "blocks_done": len(blocks) - len(missing),
                       "blocks_total": len(blocks)})
    result.sort(key=lambda r: (r["deadline"], r["subject_fio"].casefold(), r["assignment_id"]))
    return result


def get_assignment(con, assignment_id, viewer):
    g = _assignment(con, assignment_id)
    # чужая анкета для не-админа — «не найдена»: её существование не раскрывается
    if not g:
        raise NotFound("Анкета не найдена")
    owner = g["user_id"] == viewer["id"]
    if not viewer.get("is_admin") and (not owner or not _editable(g)):
        raise NotFound("Анкета не найдена")
    return {"assignment_id": g["id"], "subject_fio": g["subject_fio"], "role_title": ROLES[g["role"]],
            "deadline": g["deadline"], "is_manager": bool(g["is_manager"]), "status": g["status"],
            "editable": owner and _editable(g), "answers": _answers(con, assignment_id)}


def save_answer(con, assignment_id, user_id, key, value):
    with _write(con):
        g = _own_editable(con, assignment_id, user_id)
        value = check_answer(key, value, bool(g["is_manager"]))
        if value is None:
            con.execute("DELETE FROM answers WHERE assignment_id=? AND key=?", (assignment_id, key))
        else:
            con.execute("INSERT INTO answers (assignment_id, key, value, updated_at) VALUES (?,?,?,?) "
                        "ON CONFLICT (assignment_id, key) DO UPDATE SET value=excluded.value, "
                        "updated_at=excluded.updated_at", (assignment_id, key, value, now()))


def submit(con, assignment_id, user_id):
    with _write(con):
        g = _own_editable(con, assignment_id, user_id)
        missing = _missing_blocks(_answers(con, assignment_id), bool(g["is_manager"]))
        if missing:
            raise BadRequest("Не заполнены блоки: " + ", ".join(missing))
        con.execute("UPDATE assignments SET status='submitted', submitted_at=? WHERE id=?", (now(), assignment_id))
        if b24queue.has_task(con, assignment_id):
            b24queue.enqueue(con, assignment_id, "complete")


def return_for_revision(con, assignment_id):
    with _write(con):
        g = _assignment(con, assignment_id)
        if not g:
            raise NotFound("Анкета не найдена")
        if g["status"] != "submitted" or g["assessment_status"] != "active":
            raise Conflict("Вернуть можно только отправленную анкету, пока оценка идёт")
        con.execute("UPDATE assignments SET status='draft', submitted_at=NULL, returned_at=? WHERE id=?",
                    (now(), assignment_id))
        if b24queue.has_task(con, assignment_id):
            b24queue.enqueue(con, assignment_id, "renew")
            b24queue.enqueue(con, assignment_id, "comment", {"text": "Анкета возвращена на доработку"})
