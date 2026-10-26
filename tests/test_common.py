"""Тесты общих функций: схема базы, ежедневная копия и пример настроек. Запуск из корня: python -m unittest discover tests"""
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.common import ROOT, SCHEMA, backup_db, connect  # noqa: E402


class CommonTest(unittest.TestCase):
    def test_schema_creates_all_tables(self):
        con = connect(":memory:")
        names = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"users", "sessions", "assessments", "assignments", "answers", "b24_ops", "reminders"} <= names)

    def test_connect_is_idempotent(self):
        con = connect(":memory:")
        con.executescript(SCHEMA)

    def test_daily_backup(self):
        con = connect(":memory:")
        con.execute("INSERT INTO users (fio, login, pwd_hash, salt, created_at) VALUES ('Иванов', 'ivanov', 'h', 's', 'x')")
        con.commit()
        day = dt.date(2026, 10, 26)
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "backups")  # каталога ещё нет — создаётся
            path = backup_db(con, folder, day)
            self.assertEqual(path, os.path.join(folder, "360-2026-10-26.sqlite"))
            copy = sqlite3.connect(path)
            self.assertEqual(copy.execute("SELECT login FROM users").fetchall(), [("ivanov",)])
            copy.close()
            # копия за день уже есть — вторая не снимается
            self.assertIsNone(backup_db(con, folder, day))
            # хранятся 14 дней; чужие файлы не трогаем
            for name in ("360-2026-10-11.sqlite", "360-2026-10-12.sqlite", "360-2026-99-99.sqlite", "notes.txt"):
                open(os.path.join(folder, name), "w").close()
            self.assertEqual(backup_db(con, folder, dt.date(2026, 10, 26)), None)
            self.assertEqual(sorted(os.listdir(folder)), ["360-2026-10-12.sqlite", "360-2026-10-26.sqlite",
                                                          "360-2026-99-99.sqlite", "notes.txt"])
        con.close()

    def test_backup_removes_stale_temporary_copies(self):
        # копию оборвали (выключили компьютер) — её временный файл не должен копиться годами
        con = connect(":memory:")
        day = dt.date(2026, 10, 26)
        with tempfile.TemporaryDirectory() as folder:
            backup_db(con, folder, day)
            for name in ("360-2026-10-25.sqlite.tmp", "360-2026-09-01.sqlite.tmp", "360-2026-10-26.sqlite.tmp",
                         "360-2026-10-27.sqlite.tmp", "other.sqlite.tmp"):
                open(os.path.join(folder, name), "w").close()
            self.assertIsNone(backup_db(con, folder, day))
            # сегодняшний и завтрашний временные файлы могут быть чужой копией в работе; чужие имена не трогаем
            self.assertEqual(sorted(os.listdir(folder)), ["360-2026-10-26.sqlite", "360-2026-10-26.sqlite.tmp",
                                                          "360-2026-10-27.sqlite.tmp", "other.sqlite.tmp"])
        con.close()

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
