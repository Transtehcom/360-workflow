import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth, users  # noqa: E402
from app.common import connect  # noqa: E402


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")

    def test_password_roundtrip_and_case_insensitive_login(self):
        users.create_user(self.con, "Новоселец Иван", "Novoselec", "secret1")
        row = auth.check_password(self.con, "novoselec", "secret1")
        self.assertIsNotNone(row)
        self.assertEqual(row["login"], "novoselec")
        self.assertIsNotNone(auth.check_password(self.con, "NOVOSELEC", "secret1"))
        self.assertIsNone(auth.check_password(self.con, "novoselec", "wrong"))
        self.assertIsNone(auth.check_password(self.con, "nobody", "secret1"))

    def test_hash_is_pbkdf2_with_hex_salt(self):
        h, salt = auth.hash_password("secret1")
        self.assertEqual(len(salt), 32)
        self.assertEqual(auth.hash_password("secret1", salt), (h, salt))
        self.assertNotEqual(auth.hash_password("secret2", salt)[0], h)

    def test_inactive_user_cannot_login_and_loses_session(self):
        users.create_user(self.con, "Админ", "boss", "secret1", is_admin=True)
        uid = users.create_user(self.con, "Иванов Иван", "ivanov", "secret1")
        token = auth.new_session(self.con, uid, 12)
        self.assertEqual(auth.session_user(self.con, token)["id"], uid)
        users.update_user(self.con, uid, active=False)
        self.assertIsNone(auth.check_password(self.con, "ivanov", "secret1"))
        self.assertIsNone(auth.session_user(self.con, token))

    def test_session_expiry(self):
        uid = users.create_user(self.con, "Иванов Иван", "ivanov", "secret1")
        token = auth.new_session(self.con, uid, -1)
        self.assertIsNone(auth.session_user(self.con, token))

    def test_new_session_removes_expired_ones(self):
        uid = users.create_user(self.con, "Иванов Иван", "ivanov", "secret1")
        old = auth.new_session(self.con, uid, -1)
        auth.new_session(self.con, uid, 12)
        self.assertIsNone(self.con.execute("SELECT 1 FROM sessions WHERE token=?", (old,)).fetchone())

    def test_session_user_without_token_and_drop(self):
        uid = users.create_user(self.con, "Иванов Иван", "ivanov", "secret1")
        self.assertIsNone(auth.session_user(self.con, None))
        self.assertIsNone(auth.session_user(self.con, ""))
        self.assertIsNone(auth.session_user(self.con, "unknown"))
        token = auth.new_session(self.con, uid, 12)
        auth.drop_session(self.con, token)
        self.assertIsNone(auth.session_user(self.con, token))
        auth.drop_session(self.con, None)

    def test_login_guard(self):
        g = auth.LoginGuard()
        k = ("1.2.3.4", "ivanov")
        for _ in range(9):
            g.failed(k)
        self.assertFalse(g.blocked(k))
        g.failed(k)
        self.assertTrue(g.blocked(k))
        self.assertFalse(g.blocked(("1.2.3.4", "petrov")))
        g.reset(k)
        self.assertFalse(g.blocked(k))


class LoginGuardSweepTest(unittest.TestCase):
    def test_login_guard_sweeps_expired_keys_of_all_logins(self):
        g = auth.LoginGuard()
        with mock.patch("app.auth.time.monotonic", return_value=1000.0):
            for i in range(auth.SWEEP_FROM + 1):  # ключи, которые больше никто не проверит
                g.failed(("1.2.3.4", f"login{i}"))
            self.assertEqual(len(g.fails), auth.SWEEP_FROM + 1)  # свежие ключи чистка не трогает
        with mock.patch("app.auth.time.monotonic", return_value=1000.0 + auth.LOGIN_WINDOW - 1):
            g.failed(("1.2.3.4", "fresh-a"))
            self.assertEqual(len(g.fails), auth.SWEEP_FROM + 2)  # окно ещё не прошло
        with mock.patch("app.auth.time.monotonic", return_value=1000.0 + auth.LOGIN_WINDOW + auth.SWEEP_EVERY + 10):
            g.failed(("1.2.3.4", "fresh-b"))
        self.assertEqual(sorted(g.fails), [("1.2.3.4", "fresh-a"), ("1.2.3.4", "fresh-b")])


if __name__ == "__main__":
    unittest.main()
