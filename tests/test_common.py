"""Тесты общих функций: схема базы и пример настроек. Запуск из корня: python -m unittest discover tests"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.common import ROOT, SCHEMA, connect  # noqa: E402


class CommonTest(unittest.TestCase):
    def test_schema_creates_all_tables(self):
        con = connect(":memory:")
        names = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"users", "sessions", "assessments", "assignments", "answers", "b24_ops", "reminders"} <= names)

    def test_connect_is_idempotent(self):
        con = connect(":memory:")
        con.executescript(SCHEMA)

    def test_example_config_has_defaults(self):
        with open(os.path.join(ROOT, "config.example.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["server"]["port"], 8082)
        self.assertEqual(cfg["server"]["public_url"], "https://360.transtehcom.ru:8443")
        self.assertEqual(cfg["reminder_days"], [7, 12])
        self.assertEqual(cfg["default_deadline_days"], 14)
        self.assertEqual(cfg["bitrix24"]["webhook"], "")


if __name__ == "__main__":
    unittest.main()
