import datetime as dt
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import assessments as asm, users  # noqa: E402
from app.common import connect, today  # noqa: E402
from app.questionnaire import blocks_for, score_keys  # noqa: E402
from app.report import build_report, report_columns  # noqa: E402
from xlsx_read import read_cells  # noqa: E402

AT = dt.datetime(2026, 10, 26, 9, 5)
SUMMARY, OPEN_Q, COMMENTS = "Сводная таблица", "Открытые вопросы", "Комментарии"
O1_TEXT = "Провёл сложные переговоры\nс клиентом"
COMMENT_TEXT = "Чётко излагает мысли"


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.nat = users.create_user(self.con, "Наталья", "nat", "secret1", is_admin=True)
        self.ivanov = users.create_user(self.con, "Иванов Пётр", "ivanov", "secret1")
        self.petrov = users.create_user(self.con, "Петров Иван", "petrov", "secret1")
        self.sidorova = users.create_user(self.con, "Сидорова Анна", "sidorova", "secret1")
        self.client = users.create_user(self.con, "Клиентов Кирилл", "client", "secret1")
        self.aid = self.create()
        self.fill(self.ivanov, 4)
        self.fill(self.petrov, 2, {"O1": O1_TEXT, "c:B": COMMENT_TEXT, "extra": "Спасибо за работу"})
        self.fill(self.sidorova, 3)
        # черновик клиента: не должен попасть в файл
        g = self.assignment(self.client)
        asm.save_answer(self.con, g, self.client, "A1", 5)
        asm.save_answer(self.con, g, self.client, "O1", "ЧЕРНОВИК")

    def create(self, is_manager=False, respondents=None):
        if respondents is None:
            respondents = [(self.ivanov, "self"), (self.petrov, "peer"), (self.sidorova, "peer"),
                           (self.client, "client")]
        deadline = (today() + dt.timedelta(days=14)).isoformat()
        return asm.create_assessment(self.con, self.ivanov, is_manager, deadline, self.nat, respondents)

    def assignment(self, user_id, assessment_id=None):
        return self.con.execute("SELECT id FROM assignments WHERE assessment_id=? AND user_id=?",
                                (assessment_id or self.aid, user_id)).fetchone()["id"]

    def fill(self, user_id, score, texts=None, assessment_id=None, is_manager=False):
        g = self.assignment(user_id, assessment_id)
        for key in score_keys(is_manager):
            asm.save_answer(self.con, g, user_id, key, score)
        for key, text in (texts or {}).items():
            asm.save_answer(self.con, g, user_id, key, text)
        asm.submit(self.con, g, user_id)

    def sheets(self, assessment_id=None):
        return read_cells(build_report(self.con, assessment_id or self.aid, AT)[1])

    def test_sheet_names_and_order(self):
        self.assertEqual(list(self.sheets()), [SUMMARY, OPEN_Q, COMMENTS])

    def test_header_row(self):
        s = self.sheets()[SUMMARY]
        self.assertEqual(s["B3"], "Компетенция")
        self.assertEqual(s["C3"], "Индикатор")
        self.assertEqual(s["D3"], "Иванов Пётр\n(самооценка)")
        self.assertEqual(s["E3"], "Петров Иван\n(коллега)")
        self.assertEqual(s["F3"], "Сидорова Анна\n(коллега)")
        self.assertEqual(s["G3"], "Средняя оценка")
        self.assertEqual(s["H3"], "Разрыв (самооценка − средняя)")
        self.assertNotIn("I3", s)
        self.assertFalse(any("Клиентов" in v for v in s.values()))
        self.assertEqual([c["header"] for c in report_columns(self.con, self.aid)],
                         ["Иванов Пётр\n(самооценка)", "Петров Иван\n(коллега)", "Сидорова Анна\n(коллега)"])

    def test_rows_follow_template(self):
        s = self.sheets()[SUMMARY]
        self.assertEqual(s["B4"], "Бизнес-мышление")
        self.assertNotIn("C4", s)
        self.assertEqual(s["B5"], "Бизнес-мышление")
        self.assertEqual(s["C5"], "Понимание бизнес-показателей")
        self.assertEqual(s["D5"], "4")
        self.assertEqual(s["E5"], "2")
        self.assertEqual(s["F5"], "3")
        self.assertEqual(s["G5"], "=AVERAGE(E5:F5)")
        self.assertEqual(s["H5"], "=D5-G5")
        self.assertEqual(s["D4"], "=AVERAGE(D5:D9)")
        self.assertEqual(s["G4"], "=AVERAGE(E4:F4)")
        self.assertEqual(s["H4"], "=D4-G4")
        indicators = [r for r in range(4, 200) if f"C{r}" in s]
        self.assertEqual(len(indicators), 50)
        in_h = [s[f"C{r}"] for r in indicators if s[f"B{r}"] == "Взаимодействие"]
        self.assertEqual(len(in_h), 7)
        self.assertEqual(in_h.count("Обратная связь"), 1)
        self.assertNotIn("Управленческие качества", [v for k, v in s.items() if k.startswith("B")])
        self.assertEqual(s["B4"], blocks_for(False)[0]["export_title"])

    def test_manager_assessment_has_block_m(self):
        aid = self.create(is_manager=True, respondents=[(self.ivanov, "self"), (self.petrov, "peer")])
        self.fill(self.ivanov, 4, assessment_id=aid, is_manager=True)
        s = self.sheets(aid)[SUMMARY]
        titles = [v for k, v in s.items() if k.startswith("B") and k != "B1" and int(k[1:]) >= 4 and f"C{k[1:]}" not in s]
        self.assertEqual(titles[-1], "Управленческие качества")
        self.assertEqual(len([k for k in s if k.startswith("C") and int(k[1:]) >= 4]), 54)

    def test_only_self_submitted(self):
        aid = self.create(respondents=[(self.ivanov, "self"), (self.petrov, "peer")])
        self.fill(self.ivanov, 4, assessment_id=aid)
        s = self.sheets(aid)[SUMMARY]
        self.assertEqual(s["E3"], "Средняя оценка")
        self.assertEqual(s["F3"], "Разрыв (самооценка − средняя)")
        self.assertEqual(s["E5"], "=D5")
        self.assertEqual(s["F5"], "=D5-E5")
        self.assertEqual(s["E4"], "=D4")

    def test_without_self(self):
        aid = self.create(respondents=[(self.petrov, "peer"), (self.sidorova, "peer")])
        self.fill(self.petrov, 2, assessment_id=aid)
        s = self.sheets(aid)[SUMMARY]
        self.assertEqual(s["D3"], "Петров Иван\n(коллега)")
        self.assertEqual(s["E3"], "Средняя оценка")
        self.assertEqual(s["E5"], "=AVERAGE(D5:D5)")
        self.assertNotIn("F3", s)

    def test_nobody_submitted(self):
        aid = self.create(respondents=[(self.ivanov, "self"), (self.petrov, "peer")])
        sheets = self.sheets(aid)
        self.assertEqual(list(sheets), [SUMMARY, OPEN_Q, COMMENTS])
        s = sheets[SUMMARY]
        self.assertEqual(s.get("D3", ""), "")
        self.assertNotIn("Средняя оценка", s.values())
        self.assertFalse(any(v.startswith("=") for v in s.values()))
        self.assertEqual(sheets[OPEN_Q]["B3"], "Открытая обратная связь")
        self.assertIn("заполнили 0 из 2", s["B1"])

    def test_open_questions_and_comments(self):
        sheets = self.sheets()
        o = sheets[OPEN_Q]
        self.assertEqual(o["B3"], "Открытая обратная связь")
        self.assertEqual(o["C3"], "Иванов Пётр\n(самооценка)")
        self.assertEqual(o["D3"], "Петров Иван\n(коллега)")
        self.assertEqual(o["A4"], "1")
        self.assertTrue(o["B4"].startswith("Приведите пример"))
        self.assertEqual(o["D4"], O1_TEXT)
        self.assertNotIn("C4", o)
        self.assertNotIn("ЧЕРНОВИК", o.values())
        self.assertEqual(o["B11"], "Дополнительные комментарии")
        self.assertEqual(o["D11"], "Спасибо за работу")
        c = sheets[COMMENTS]
        self.assertEqual((c["B3"], c["C3"], c["D3"]), ("Компетенция", "ФИО (роль)", "Комментарий"))
        rows = {}
        for addr, v in c.items():
            rows.setdefault(int(addr[1:]), {})[addr[0]] = v
        table = [[r["B"], r["C"], r["D"]] for n, r in sorted(rows.items()) if n >= 4]
        self.assertEqual(table, [["Коммуникативные навыки", "Петров Иван (коллега)", COMMENT_TEXT],
                                 ["Дополнительные комментарии", "Петров Иван (коллега)", "Спасибо за работу"]])

    def test_filename(self):
        con = self.con
        con.execute("UPDATE users SET fio=? WHERE id=?", ('Иванов "Пётр"', self.ivanov))
        name, data = build_report(con, self.aid, AT)
        self.assertEqual(name, "Оценка 360 — Иванов _Пётр_ — 26.10.2026.xlsx")
        self.assertTrue(data.startswith(b"PK"))

    def test_title_row(self):
        s = self.sheets()[SUMMARY]
        deadline = (today() + dt.timedelta(days=14)).strftime("%d.%m.%Y")
        self.assertTrue(s["B1"].startswith("Оценка 360: Иванов Пётр · срок "))
        self.assertIn("заполнили 3 из 4", s["B1"])
        self.assertEqual(s["B1"], f"Оценка 360: Иванов Пётр · срок {deadline} · заполнили 3 из 4 · "
                                  "выгружено 26.10.2026 09:05")

    def test_no_empty_ranges_in_formulas(self):
        for sheet in self.sheets().values():
            for v in sheet.values():
                if v.startswith("="):
                    self.assertTrue(re.fullmatch(r"=([A-Z]+\d+|AVERAGE\([A-Z]+\d+:[A-Z]+\d+\)|[A-Z]+\d+-[A-Z]+\d+)", v), v)


if __name__ == "__main__":
    unittest.main()
