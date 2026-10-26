import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.common import BadRequest  # noqa: E402
from app.questionnaire import (  # noqa: E402
    BLOCKS, EXTRA_KEY, EXTRA_TITLE, INSTRUCTION, OPEN_QUESTIONS, ROLES, SCALE,
    blocks_for, check_answer, score_keys, text_keys,
)


def block(key):
    return next(b for b in BLOCKS if b["key"] == key)


class QuestionnaireTest(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(len(score_keys(True)), 54)
        self.assertEqual(len(score_keys(False)), 50)
        self.assertEqual([b["key"] for b in blocks_for(False)], list("ABCDEFGHIK"))
        self.assertEqual([b["key"] for b in blocks_for(True)], list("ABCDEFGHIKM"))
        self.assertTrue(block("M")["manager_only"])

    def test_keys_unique_and_prefixed(self):
        keys = []
        for b in BLOCKS:
            for item in b["items"]:
                self.assertTrue(item["key"].startswith(b["key"]), item["key"])
                keys.append(item["key"])
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys, score_keys(True))

    def test_h_block_has_feedback_row(self):
        self.assertEqual(
            [i["short"] for i in block("H")["items"]],
            ["Конструктивность", "Результативность", "Изменения", "Обратная связь",
             "Конфликтность", "Поддержка", "Вклад в атмосферу"])

    def test_export_titles(self):
        self.assertEqual(block("E")["title"], "Работа с искусственным интеллектом (ИИ)")
        self.assertEqual(block("E")["export_title"], "Искусственный интеллект")
        self.assertEqual(block("H")["export_title"], "Взаимодействие")
        self.assertEqual(block("M")["title"], "Управленческие качества")

    def test_roles_scale_texts(self):
        self.assertEqual(list(ROLES), ["self", "manager", "peer", "report", "client"])
        self.assertEqual(ROLES["client"], "клиент/партнёр")
        self.assertEqual([n for n, _ in SCALE], [1, 2, 3, 4, 5])
        self.assertEqual(SCALE[0][1], "Никогда / Совершенно не проявляет (требуется немедленное развитие)")
        self.assertTrue(INSTRUCTION.startswith("Для каждого утверждения отметьте"))
        self.assertEqual(EXTRA_KEY, "extra")
        self.assertEqual(EXTRA_TITLE, "Дополнительные комментарии")

    def test_open_questions(self):
        self.assertEqual(len(OPEN_QUESTIONS), 7)
        self.assertEqual([q["key"] for q in OPEN_QUESTIONS], ["O%d" % i for i in range(1, 8)])
        self.assertTrue(OPEN_QUESTIONS[0]["text"].startswith(
            "Приведите пример ситуации, когда сотрудник проявил себя с лучшей стороны"))

    def test_text_keys(self):
        self.assertEqual(text_keys(False), {"c:" + k for k in "ABCDEFGHIK"} | {"O%d" % i for i in range(1, 8)} | {"extra"})
        self.assertIn("c:M", text_keys(True))

    def test_check_answer(self):
        self.assertEqual(check_answer("A1", 5, False), "5")
        self.assertEqual(check_answer("A1", "3", False), "3")
        for bad in (0, 6, "x", True, 2.5, "", "7"):
            if bad == "":
                continue
            with self.assertRaises(BadRequest, msg=repr(bad)):
                check_answer("A1", bad, False)
        with self.assertRaises(BadRequest):
            check_answer("M1", 4, False)
        self.assertEqual(check_answer("M1", 4, True), "4")
        self.assertEqual(check_answer("c:A", "  текст ", False), "  текст ")
        self.assertIsNone(check_answer("O1", "", False))
        self.assertIsNone(check_answer("A1", None, False))
        self.assertIsNone(check_answer("A1", "", False))
        self.assertEqual(check_answer("O1", "x" * 5000, False), "x" * 5000)
        with self.assertRaises(BadRequest):
            check_answer("O1", "x" * 5001, False)
        with self.assertRaises(BadRequest):
            check_answer("Z9", 1, False)
        with self.assertRaises(BadRequest):
            check_answer("c:M", "текст", False)
        with self.assertRaises(BadRequest):
            check_answer("O1", 5, False)
        self.assertEqual(check_answer("extra", "да", False), "да")


if __name__ == "__main__":
    unittest.main()
