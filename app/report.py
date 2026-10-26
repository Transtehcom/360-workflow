"""Выгрузка оценки в .xlsx по шаблону заказчика: сводная таблица, открытые вопросы, комментарии.

В файл попадают только отправленные анкеты. Средние и разрыв — формулы Excel (AVERAGE), чтобы
правка оценки в файле пересчитывала итоги."""
import datetime as dt
import re

from .assessments import get_assessment
from .questionnaire import EXTRA_KEY, EXTRA_TITLE, OPEN_QUESTIONS, blocks_for
from .xlsx import Cell, Sheet, col_letter, write_workbook

FIRST_COL = 4            # D: первая колонка оценивающих (B — компетенция, C — индикатор)
FIRST_ROW = 4            # первая строка данных (строка 3 — шапка)
AVG_TITLE = "Средняя оценка"
GAP_TITLE = "Разрыв (самооценка − средняя)"
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def _date(value):
    """ГГГГ-ММ-ДД (или дата) -> ДД.ММ.ГГГГ."""
    if isinstance(value, str):
        value = dt.date.fromisoformat(value)
    return value.strftime("%d.%m.%Y")


def report_columns(con, assessment_id):
    """Отправленные анкеты в порядке колонок выгрузки: роль, затем ФИО."""
    return [{"assignment_id": r["assignment_id"], "fio": r["fio"], "role": r["role"],
             "header": f"{r['fio']}\n({r['role_title']})", "role_title": r["role_title"]}
            for r in get_assessment(con, assessment_id)["respondents"] if r["status"] == "submitted"]


def _answers(con, columns):
    """{assignment_id: {ключ: значение}} по отправленным анкетам."""
    result = {c["assignment_id"]: {} for c in columns}
    for r in con.execute("SELECT assignment_id, key, value FROM answers"):
        if r["assignment_id"] in result:
            result[r["assignment_id"]][r["key"]] = r["value"]
    return result


def _summary_sheet(title, is_manager, columns, answers):
    n = len(columns)
    cells = {(1, 2): Cell(title, "title"),
             (3, 2): Cell("Компетенция", "header_left"), (3, 3): Cell("Индикатор", "header_left")}
    for i, c in enumerate(columns):
        cells[(3, FIRST_COL + i)] = Cell(c["header"], "header")
    has_self = n > 0 and columns[0]["role"] == "self"
    self_col = FIRST_COL if has_self else None
    others = range(FIRST_COL + (1 if has_self else 0), FIRST_COL + n)  # колонки оценивающих без самооценки
    avg_col = FIRST_COL + n if n else None
    gap_col = avg_col + 1 if has_self else None
    if avg_col:
        cells[(3, avg_col)] = Cell(AVG_TITLE, "header")
    if gap_col:
        cells[(3, gap_col)] = Cell(GAP_TITLE, "header")

    def totals(row, num_style):
        """Колонки «Средняя» и «Разрыв» строки row."""
        if not avg_col:
            return
        if others:
            avg = f"AVERAGE({col_letter(others[0])}{row}:{col_letter(others[-1])}{row})"
        else:
            avg = f"{col_letter(self_col)}{row}"  # других оценок нет — берётся самооценка
        cells[(row, avg_col)] = Cell(formula=avg, style=num_style)
        if gap_col:
            cells[(row, gap_col)] = Cell(formula=f"{col_letter(self_col)}{row}-{col_letter(avg_col)}{row}",
                                         style=num_style)

    row = FIRST_ROW
    for block in blocks_for(is_manager):
        head, items = row, block["items"]
        cells[(head, 2)] = Cell(block["export_title"], "comp")
        cells[(head, 3)] = Cell(None, "comp")
        for i in range(n):
            letter = col_letter(FIRST_COL + i)
            cells[(head, FIRST_COL + i)] = Cell(
                formula=f"AVERAGE({letter}{head + 1}:{letter}{head + len(items)})", style="comp_num")
        totals(head, "comp_num")
        for k, item in enumerate(items, 1):
            r = head + k
            cells[(r, 2)] = Cell(block["export_title"], "text")
            cells[(r, 3)] = Cell(item["short"], "text")
            for i, c in enumerate(columns):
                value = answers[c["assignment_id"]].get(item["key"])
                cells[(r, FIRST_COL + i)] = Cell(None if value is None else int(value), "num")
            totals(r, "num")
        row = head + len(items) + 1

    widths = {2: 28, 3: 28}
    widths.update({FIRST_COL + i: 16 for i in range(n)})
    for col in (avg_col, gap_col):
        if col:
            widths[col] = 14
    return Sheet("Сводная таблица", cells, col_widths=widths, row_heights={3: 47.25})


def _open_questions_sheet(title, columns, answers):
    cells = {(1, 2): Cell(title, "title"), (3, 2): Cell("Открытая обратная связь", "header_left")}
    for i, c in enumerate(columns):
        cells[(3, 3 + i)] = Cell(c["header"], "header")
    rows = [(str(num), q["text"], q["key"]) for num, q in enumerate(OPEN_QUESTIONS, 1)]
    rows.append((None, EXTRA_TITLE, EXTRA_KEY))
    for r, (num, text, key) in enumerate(rows, FIRST_ROW):
        if num is not None:
            cells[(r, 1)] = Cell(int(num), "text")
        cells[(r, 2)] = Cell(text, "text")
        for i, c in enumerate(columns):
            cells[(r, 3 + i)] = Cell(answers[c["assignment_id"]].get(key), "text")
    widths = {2: 44}
    widths.update({3 + i: 40 for i in range(len(columns))})
    return Sheet("Открытые вопросы", cells, col_widths=widths)


def _comments_sheet(title, is_manager, columns, answers):
    cells = {(1, 2): Cell(title, "title"), (3, 2): Cell("Компетенция", "header_left"),
             (3, 3): Cell("ФИО (роль)", "header_left"), (3, 4): Cell("Комментарий", "header_left")}
    sources = [(b["export_title"], "c:" + b["key"]) for b in blocks_for(is_manager)] + [(EXTRA_TITLE, EXTRA_KEY)]
    row = FIRST_ROW
    for competence, key in sources:
        for c in columns:
            text = answers[c["assignment_id"]].get(key)
            if text and text.strip():
                cells[(row, 2)] = Cell(competence, "text")
                cells[(row, 3)] = Cell(f"{c['fio']} ({c['role_title']})", "text")
                cells[(row, 4)] = Cell(text, "text")
                row += 1
    return Sheet("Комментарии", cells, col_widths={2: 28, 3: 28, 4: 80})


def build_report(con, assessment_id, at=None):
    """Имя файла и содержимое .xlsx с выгрузкой оценки; at — момент выгрузки (по умолчанию сейчас)."""
    at = at or dt.datetime.now()
    a = get_assessment(con, assessment_id)
    columns = report_columns(con, assessment_id)
    answers = _answers(con, columns)
    title = (f"Оценка 360: {a['subject_fio']} · срок {_date(a['deadline'])} · "
             f"заполнили {a['submitted']} из {a['total']} · выгружено {_date(at)} {at:%H:%M}")
    data = write_workbook([
        _summary_sheet(title, a["is_manager"], columns, answers),
        _open_questions_sheet(title, columns, answers),
        _comments_sheet(title, a["is_manager"], columns, answers),
    ])
    name = _BAD_NAME_CHARS.sub("_", f"Оценка 360 — {a['subject_fio']} — {_date(at)}.xlsx")
    return name, data
