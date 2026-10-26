import datetime as dt
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import assessments as asm, b24queue, users  # noqa: E402
from app.common import BadRequest, Conflict, NotFound, connect, today  # noqa: E402
from app.questionnaire import score_keys  # noqa: E402


def day(offset):
    return (today() + dt.timedelta(days=offset)).isoformat()


class AssessmentsTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")
        self.addCleanup(self.con.close)
        self.nat = users.create_user(self.con, "Наталья", "nat", "secret1", is_admin=True)
        self.ivanov = users.create_user(self.con, "Иванов Иван", "ivanov", "secret1", b24_user_id=11)
        self.petrov = users.create_user(self.con, "Петров Пётр", "petrov", "secret1", b24_user_id=12)
        self.client = users.create_user(self.con, "Клиентов Кирилл", "client", "secret1")
        self.aid = self.create()
        self.a_self, self.a_peer, self.a_client = (self.assignment(u) for u in (self.ivanov, self.petrov, self.client))

    def create(self, is_manager=False, respondents=None, subject=None, deadline=None):
        if respondents is None:
            respondents = [(self.ivanov, "self"), (self.petrov, "peer"), (self.client, "client")]
        return asm.create_assessment(self.con, subject or self.ivanov, is_manager, deadline or day(14),
                                     self.nat, respondents)

    def assignment(self, user_id, assessment_id=None):
        return self.con.execute("SELECT id FROM assignments WHERE assessment_id=? AND user_id=?",
                                (assessment_id or self.aid, user_id)).fetchone()["id"]

    def viewer(self, user_id):
        return dict(self.con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def ops(self, op=None):
        rows = self.con.execute("SELECT assignment_id, op, payload FROM b24_ops ORDER BY id").fetchall()
        return [(r["assignment_id"], r["op"], json.loads(r["payload"])) for r in rows if op in (None, r["op"])]

    def fill(self, assignment_id, user_id, keys):
        for k in keys:
            asm.save_answer(self.con, assignment_id, user_id, k, 4)

    def test_create_validations(self):
        with self.assertRaises(BadRequest):
            self.create(respondents=[(self.petrov, "self")])
        with self.assertRaises(BadRequest):
            self.create(respondents=[(self.petrov, "peer"), (self.petrov, "manager")])
        with self.assertRaises(BadRequest):
            self.create(deadline=day(-1))
        with self.assertRaises(BadRequest):
            self.create(deadline="31.12.2099")
        with self.assertRaises(BadRequest):
            self.create(respondents=[])
        with self.assertRaises(BadRequest):
            self.create(respondents=[(self.ivanov, "peer")])
        with self.assertRaises(BadRequest):
            self.create(respondents=[(self.petrov, "boss")])
        with self.assertRaises(BadRequest):
            self.create(respondents=[(999, "peer")])
        users.update_user(self.con, self.petrov, active=False)
        with self.assertRaises(BadRequest):
            self.create(respondents=[(self.petrov, "peer")])
        with self.assertRaises(BadRequest):
            self.create(subject=self.petrov, respondents=[(self.ivanov, "peer")])
        self.create(deadline=day(0), respondents=[(self.ivanov, "self")])

    def test_create_enqueues_tasks_only_for_linked(self):
        self.assertEqual(self.ops(), [(self.a_self, "create", {}), (self.a_peer, "create", {})])
        self.assertEqual(b24queue.task_status(self.con, self.a_client), "не связан с Битрикс24")
        self.assertEqual(b24queue.task_status(self.con, self.a_peer), "ставится…")
        a = asm.get_assessment(self.con, self.aid)
        self.assertEqual([r["role"] for r in a["respondents"]], ["self", "peer", "client"])
        self.assertEqual(a["respondents"][1]["role_title"], "коллега")
        self.assertEqual((a["subject_fio"], a["is_manager"], a["submitted"], a["total"]),
                         ("Иванов Иван", False, 0, 3))

    def test_task_status_labels(self):
        self.con.execute("UPDATE b24_ops SET last_error='timeout', attempts=3 WHERE assignment_id=?", (self.a_peer,))
        self.assertEqual(b24queue.task_status(self.con, self.a_peer), "ошибка Битрикс24: timeout")
        self.assertIs(b24queue.has_task(self.con, self.a_peer), True)
        self.con.execute("UPDATE b24_ops SET attempts=? WHERE assignment_id=?", (b24queue.MAX_ATTEMPTS, self.a_peer))
        self.assertEqual(b24queue.task_status(self.con, self.a_peer), "задача не поставлена")
        self.assertIs(b24queue.has_task(self.con, self.a_peer), False)
        self.con.execute("UPDATE b24_ops SET done_at='x' WHERE assignment_id=?", (self.a_peer,))
        self.con.execute("UPDATE assignments SET b24_task_id=500 WHERE id=?", (self.a_peer,))
        self.assertEqual(b24queue.task_status(self.con, self.a_peer), "задача поставлена")
        self.assertIs(b24queue.has_task(self.con, self.a_peer), True)
        self.assertIs(b24queue.has_task(self.con, self.a_client), False)
        with self.assertRaises(ValueError):
            b24queue.enqueue(self.con, self.a_peer, "delete")

    def test_respondent_sees_only_own(self):
        mine = asm.my_assignments(self.con, self.petrov)
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["assignment_id"], self.a_peer)
        self.assertEqual((mine[0]["subject_fio"], mine[0]["role_title"]), ("Иванов Иван", "коллега"))
        with self.assertRaises(NotFound):
            asm.get_assignment(self.con, self.a_self, self.viewer(self.petrov))
        with self.assertRaises(NotFound):
            asm.save_answer(self.con, self.a_self, self.petrov, "A1", 5)
        own = asm.get_assignment(self.con, self.a_peer, self.viewer(self.petrov))
        self.assertIs(own["editable"], True)

    def test_admin_can_view_any_but_not_edit(self):
        asm.save_answer(self.con, self.a_peer, self.petrov, "A1", "3")
        a = asm.get_assignment(self.con, self.a_peer, self.viewer(self.nat))
        self.assertIs(a["editable"], False)
        self.assertEqual(a["answers"], {"A1": "3"})
        with self.assertRaises(NotFound):
            asm.save_answer(self.con, self.a_peer, self.nat, "A2", 3)

    def test_autosave_and_progress(self):
        self.fill(self.a_peer, self.petrov, ["A1", "A2", "A3", "A4", "A5"])
        asm.save_answer(self.con, self.a_peer, self.petrov, "c:A", "пример")
        mine = asm.my_assignments(self.con, self.petrov)[0]
        self.assertEqual((mine["blocks_done"], mine["blocks_total"]), (1, 10))
        asm.save_answer(self.con, self.a_peer, self.petrov, "A1", None)
        answers = asm.get_assignment(self.con, self.a_peer, self.viewer(self.petrov))["answers"]
        self.assertNotIn("A1", answers)
        self.assertEqual(answers["c:A"], "пример")
        self.assertEqual(asm.my_assignments(self.con, self.petrov)[0]["blocks_done"], 0)

    def test_submit_requires_all_scores(self):
        self.fill(self.a_peer, self.petrov, ["A1", "A2", "A3", "A4", "A5"])
        with self.assertRaises(BadRequest) as cm:
            asm.submit(self.con, self.a_peer, self.petrov)
        self.assertIn("Не заполнены блоки: B, C", str(cm.exception))
        self.assertIn("K", str(cm.exception))
        self.fill(self.a_peer, self.petrov, score_keys(False))
        asm.submit(self.con, self.a_peer, self.petrov)
        self.assertEqual(asm.my_assignments(self.con, self.petrov), [])
        self.assertEqual(self.ops("complete"), [(self.a_peer, "complete", {})])
        self.assertEqual(asm.get_assessment(self.con, self.aid)["submitted"], 1)

    def test_double_submit_and_late_save_conflict(self):
        self.fill(self.a_peer, self.petrov, score_keys(False))
        asm.submit(self.con, self.a_peer, self.petrov)
        with self.assertRaisesRegex(Conflict, "Анкета уже отправлена или оценка закрыта"):
            asm.submit(self.con, self.a_peer, self.petrov)
        with self.assertRaisesRegex(Conflict, "Анкета уже отправлена или оценка закрыта"):
            asm.save_answer(self.con, self.a_peer, self.petrov, "A1", 1)
        self.assertEqual(len(self.ops("complete")), 1)
        with self.assertRaises(NotFound):
            asm.get_assignment(self.con, self.a_peer, self.viewer(self.petrov))

    def test_manager_block(self):
        mid = self.create(is_manager=True, respondents=[(self.petrov, "report")])
        m_peer = self.assignment(self.petrov, mid)
        asm.save_answer(self.con, m_peer, self.petrov, "M1", 5)
        self.fill(m_peer, self.petrov, score_keys(False))
        with self.assertRaisesRegex(BadRequest, "Не заполнены блоки: M"):
            asm.submit(self.con, m_peer, self.petrov)
        self.fill(m_peer, self.petrov, score_keys(True))
        asm.submit(self.con, m_peer, self.petrov)
        answers = self.con.execute("SELECT COUNT(*) FROM answers WHERE assignment_id=?", (m_peer,)).fetchone()[0]
        self.assertEqual(answers, 54)
        with self.assertRaises(BadRequest):
            asm.save_answer(self.con, self.a_peer, self.petrov, "M1", 5)

    def test_remove_rules(self):
        self.fill(self.a_self, self.ivanov, score_keys(False))
        asm.submit(self.con, self.a_self, self.ivanov)
        with self.assertRaisesRegex(Conflict, "Анкета уже отправлена — её нельзя убрать"):
            asm.remove_respondent(self.con, self.a_self)
        # без задачи: строка и ответы исчезают, в очередь ничего не ставится
        asm.save_answer(self.con, self.a_client, self.client, "A1", 2)
        before = len(self.ops())
        asm.remove_respondent(self.con, self.a_client)
        self.assertEqual(len(self.ops()), before)
        # с задачей: comment и complete с ID задачи в payload
        asm.save_answer(self.con, self.a_peer, self.petrov, "A1", 2)
        self.con.execute("UPDATE assignments SET b24_task_id=777 WHERE id=?", (self.a_peer,))
        asm.remove_respondent(self.con, self.a_peer)
        for a in (self.a_client, self.a_peer):
            self.assertIsNone(self.con.execute("SELECT 1 FROM assignments WHERE id=?", (a,)).fetchone())
            self.assertIsNone(self.con.execute("SELECT 1 FROM answers WHERE assignment_id=?", (a,)).fetchone())
        self.assertEqual(self.ops()[-2:], [
            (self.a_peer, "comment", {"text": "Вы исключены из оценки", "task_id": 777}),
            (self.a_peer, "complete", {"task_id": 777}),
        ])
        self.assertEqual(asm.get_assessment(self.con, self.aid)["total"], 1)
        with self.assertRaises(NotFound):
            asm.remove_respondent(self.con, self.a_peer)

    def test_add_respondent(self):
        other = users.create_user(self.con, "Сидоров Сидор", "sidorov", "secret1", b24_user_id=13)
        with self.assertRaises(BadRequest):
            asm.add_respondent(self.con, self.aid, self.petrov, "manager")
        with self.assertRaises(BadRequest):
            asm.add_respondent(self.con, self.aid, other, "self")
        new = asm.add_respondent(self.con, self.aid, other, "manager")
        self.assertEqual(self.ops()[-1], (new, "create", {}))
        a = asm.get_assessment(self.con, self.aid)
        self.assertEqual([r["fio"] for r in a["respondents"]],
                         ["Иванов Иван", "Сидоров Сидор", "Петров Пётр", "Клиентов Кирилл"])
        asm.cancel_assessment(self.con, self.aid)
        with self.assertRaises(Conflict):
            asm.add_respondent(self.con, self.aid, self.nat, "peer")

    def test_return_for_revision(self):
        self.fill(self.a_peer, self.petrov, score_keys(False))
        with self.assertRaises(Conflict):
            asm.return_for_revision(self.con, self.a_peer)
        asm.submit(self.con, self.a_peer, self.petrov)
        asm.return_for_revision(self.con, self.a_peer)
        self.assertEqual([m["assignment_id"] for m in asm.my_assignments(self.con, self.petrov)], [self.a_peer])
        a = asm.get_assignment(self.con, self.a_peer, self.viewer(self.petrov))
        self.assertEqual(len(a["answers"]), 50)
        self.assertEqual(self.ops()[-2:], [(self.a_peer, "renew", {}),
                                           (self.a_peer, "comment", {"text": "Анкета возвращена на доработку"})])
        row = asm.get_assessment(self.con, self.aid)["respondents"][1]
        self.assertIsNone(row["submitted_at"])
        self.assertIsNotNone(row["returned_at"])
        asm.submit(self.con, self.a_peer, self.petrov)
        asm.finish_assessment(self.con, self.aid)
        with self.assertRaises(Conflict):
            asm.return_for_revision(self.con, self.a_peer)

    def test_finish_closes_drafts(self):
        self.fill(self.a_self, self.ivanov, score_keys(False))
        asm.submit(self.con, self.a_self, self.ivanov)
        asm.remove_respondent(self.con, self.a_client)
        asm.finish_assessment(self.con, self.aid)
        statuses = {r["user_id"]: r["status"] for r in asm.get_assessment(self.con, self.aid)["respondents"]}
        self.assertEqual(statuses, {self.ivanov: "submitted", self.petrov: "closed"})
        self.assertEqual(asm.my_assignments(self.con, self.petrov), [])
        self.assertEqual(self.ops()[-2:], [(self.a_peer, "comment", {"text": "Оценка завершена"}),
                                           (self.a_peer, "complete", {})])
        with self.assertRaises(Conflict):
            asm.finish_assessment(self.con, self.aid)
        with self.assertRaises(Conflict):
            asm.cancel_assessment(self.con, self.aid)
        row = asm.list_assessments(self.con)[0]
        self.assertEqual((row["status"], row["submitted"], row["total"]), ("finished", 1, 2))
        with self.assertRaises(NotFound):
            asm.get_assignment(self.con, self.a_peer, self.viewer(self.petrov))

    def test_cancel_and_list_order(self):
        second = self.create(subject=self.petrov, respondents=[(self.client, "client")])
        asm.cancel_assessment(self.con, self.aid)
        self.assertEqual([a["id"] for a in asm.list_assessments(self.con)], [second, self.aid])
        self.assertEqual(asm.get_assessment(self.con, self.aid)["status"], "cancelled")
        self.assertEqual(self.ops("comment")[-1], (self.a_peer, "comment", {"text": "Оценка отменена"}))
        statuses = {r["status"] for r in asm.get_assessment(self.con, self.aid)["respondents"]}
        self.assertEqual(statuses, {"closed"})

    def test_closed_assessment_rejects_edits(self):
        second = self.create(subject=self.petrov, respondents=[(self.ivanov, "peer")])
        s_peer = self.assignment(self.ivanov, second)
        asm.cancel_assessment(self.con, self.aid)
        asm.finish_assessment(self.con, second)
        for assignment_id, user_id in ((self.a_peer, self.petrov), (s_peer, self.ivanov)):
            with self.subTest(assignment_id=assignment_id):
                with self.assertRaisesRegex(Conflict, "Анкета уже отправлена или оценка закрыта"):
                    asm.save_answer(self.con, assignment_id, user_id, "A1", 3)
                with self.assertRaisesRegex(Conflict, "Анкета уже отправлена или оценка закрыта"):
                    asm.submit(self.con, assignment_id, user_id)

    def test_ops_follow_task_not_link(self):
        # связь с Битрикс24 убрали после постановки задачи — задачу всё равно закрываем
        self.con.execute("UPDATE b24_ops SET done_at='x' WHERE assignment_id=?", (self.a_peer,))
        self.con.execute("UPDATE assignments SET b24_task_id=500 WHERE id=?", (self.a_peer,))
        users.update_user(self.con, self.petrov, b24_user_id=None)
        self.fill(self.a_peer, self.petrov, score_keys(False))
        asm.submit(self.con, self.a_peer, self.petrov)
        self.assertEqual(self.ops()[-1], (self.a_peer, "complete", {}))
        # связь появилась после назначения — задачи нет, ставить нечего
        users.update_user(self.con, self.client, b24_user_id=20)
        self.fill(self.a_client, self.client, score_keys(False))
        asm.submit(self.con, self.a_client, self.client)
        self.assertEqual([o for o in self.ops() if o[0] == self.a_client], [])
        # create исчерпал попытки — завершение не ставит операций, которым нечего ждать
        self.con.execute("UPDATE b24_ops SET attempts=? WHERE assignment_id=?", (b24queue.MAX_ATTEMPTS, self.a_self))
        before = len(self.ops())
        asm.finish_assessment(self.con, self.aid)
        self.assertEqual(len(self.ops()), before)

    def test_remove_unlinked_with_task(self):
        self.con.execute("UPDATE assignments SET b24_task_id=777 WHERE id=?", (self.a_peer,))
        users.update_user(self.con, self.petrov, b24_user_id=None)
        asm.remove_respondent(self.con, self.a_peer)
        self.assertEqual(self.ops()[-2:], [
            (self.a_peer, "comment", {"text": "Вы исключены из оценки", "task_id": 777}),
            (self.a_peer, "complete", {"task_id": 777}),
        ])

    def test_deadline_change_enqueues(self):
        self.fill(self.a_self, self.ivanov, score_keys(False))
        asm.submit(self.con, self.a_self, self.ivanov)
        with self.assertRaises(BadRequest):
            asm.set_deadline(self.con, self.aid, day(-1))
        asm.set_deadline(self.con, self.aid, day(20))
        self.assertEqual(self.ops("deadline"), [(self.a_peer, "deadline", {"deadline": day(20)})])
        self.assertEqual(asm.get_assessment(self.con, self.aid)["deadline"], day(20))
        asm.finish_assessment(self.con, self.aid)
        with self.assertRaises(Conflict):
            asm.set_deadline(self.con, self.aid, day(21))


if __name__ == "__main__":
    unittest.main()
