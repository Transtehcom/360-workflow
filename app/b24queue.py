"""Очередь операций Битрикс24: постановка в очередь и подпись состояния задачи.

Операции выполняет фоновый поток (process); здесь — только запись в b24_ops."""
import json

from .common import now

OPS = ("create", "deadline", "comment", "complete", "renew")
MAX_ATTEMPTS = 24  # после стольких неудачных попыток операция больше не повторяется


def enqueue(con, assignment_id, op, payload=None):
    """Ставит операцию в очередь. Не делает commit: запись уходит вместе с изменением вызывающего."""
    if op not in OPS:
        raise ValueError(f"Неизвестная операция Битрикс24: {op}")
    con.execute("INSERT INTO b24_ops (assignment_id, op, payload, created_at) VALUES (?,?,?,?)",
                (assignment_id, op, json.dumps(payload or {}, ensure_ascii=False), now()))


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
        return "нет связи с Битрикс24"
    pending = con.execute("SELECT attempts, last_error FROM b24_ops WHERE assignment_id=? AND done_at IS NULL "
                          "ORDER BY id", (assignment_id,)).fetchall()
    if any(p["attempts"] >= MAX_ATTEMPTS for p in pending):
        return "задача не поставлена"
    failed = [p for p in pending if p["last_error"]]
    if failed:
        return f"ошибка Битрикс24: {failed[0]['last_error']}"
    if pending:
        return "ставится…"
    if a["b24_task_id"] is not None:
        return "задача поставлена"
    return "задача не поставлена"
