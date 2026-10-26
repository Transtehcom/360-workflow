import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth, users  # noqa: E402
from app.common import BadRequest, Conflict, NotFound, connect  # noqa: E402


class UsersTest(unittest.TestCase):
    def setUp(self):
        self.con = connect(":memory:")

    def make(self, login="ivanov", **kw):
        return users.create_user(self.con, kw.pop("fio", "Иванов Иван"), login, kw.pop("password", "secret1"), **kw)

    def test_duplicate_login_conflict(self):
        self.make("ivanov")
        with self.assertRaisesRegex(Conflict, "Логин уже занят"):
            self.make("IVANOV", fio="Другой")

    def test_short_password_rejected(self):
        with self.assertRaises(BadRequest):
            self.make(password="12345")

    def test_bad_login_rejected(self):
        with self.assertRaises(BadRequest):
            self.make("ив ан")
        with self.assertRaises(BadRequest):
            self.make("a")

    def test_empty_fio_rejected(self):
        with self.assertRaises(BadRequest):
            self.make(fio="   ")

    def test_login_is_lowercased_and_fields_stored(self):
        uid = self.make("Ivanov.I", position=" Менеджер ", b24_user_id="15")
        u = users.get_user(self.con, uid)
        self.assertEqual(u["login"], "ivanov.i")
        self.assertEqual(u["position"], "Менеджер")
        self.assertEqual(u["b24_user_id"], 15)
        self.assertIs(u["is_admin"], False)
        self.assertIs(u["active"], True)

    def test_b24_user_id_validation(self):
        for bad in (0, -1, "abc", "1.5", 1.5, True, [1]):
            with self.subTest(bad=bad), self.assertRaises(BadRequest):
                self.make(b24_user_id=bad)
        uid = self.make("a1", b24_user_id="")
        self.assertIsNone(users.get_user(self.con, uid)["b24_user_id"])
        uid = self.make("a2", b24_user_id=7)
        self.assertEqual(users.get_user(self.con, uid)["b24_user_id"], 7)

    def test_list_users_has_no_hashes(self):
        self.make("petrov", fio="Петров Пётр")
        self.make("ivanov", fio="Иванов Иван")
        rows = users.list_users(self.con)
        self.assertEqual([r["fio"] for r in rows], ["Иванов Иван", "Петров Пётр"])
        for r in rows:
            self.assertNotIn("pwd_hash", r)
            self.assertNotIn("salt", r)
            self.assertEqual(set(r), {"id", "fio", "position", "login", "is_admin", "active", "b24_user_id"})

    def test_get_user_not_found(self):
        with self.assertRaises(NotFound):
            users.get_user(self.con, 999)

    def test_public(self):
        uid = self.make(position="Менеджер", is_admin=True)
        row = self.con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        self.assertEqual(users.public(row), {"id": uid, "fio": "Иванов Иван", "position": "Менеджер",
                                             "login": "ivanov", "is_admin": True})

    def test_password_reset(self):
        uid = self.make()
        users.update_user(self.con, uid, password="newpass")
        self.assertIsNone(auth.check_password(self.con, "ivanov", "secret1"))
        self.assertIsNotNone(auth.check_password(self.con, "ivanov", "newpass"))

    def test_empty_password_in_update_means_no_change(self):
        uid = self.make()
        users.update_user(self.con, uid, password="", fio="Иванов И.")
        self.assertIsNotNone(auth.check_password(self.con, "ivanov", "secret1"))
        self.assertEqual(users.get_user(self.con, uid)["fio"], "Иванов И.")

    def test_short_password_reset_rejected(self):
        uid = self.make()
        with self.assertRaises(BadRequest):
            users.update_user(self.con, uid, password="123")

    def test_update_unknown_user_and_field(self):
        uid = self.make()
        with self.assertRaises(NotFound):
            users.update_user(self.con, 999, fio="X")
        with self.assertRaises(BadRequest):
            users.update_user(self.con, uid, pwd_hash="x")

    def test_update_login_validated_and_unique(self):
        a = self.make("ivanov")
        self.make("petrov", fio="Петров")
        with self.assertRaises(Conflict):
            users.update_user(self.con, a, login="Petrov")
        with self.assertRaises(BadRequest):
            users.update_user(self.con, a, login="ив ан")
        users.update_user(self.con, a, login="IVANOV")  # свой же логин — не конфликт
        users.update_user(self.con, a, login="new.login")
        self.assertEqual(users.get_user(self.con, a)["login"], "new.login")

    def test_update_is_atomic_on_error(self):
        uid = self.make()
        with self.assertRaises(BadRequest):
            users.update_user(self.con, uid, fio="Новое", b24_user_id="abc")
        self.assertEqual(users.get_user(self.con, uid)["fio"], "Иванов Иван")

    def test_update_b24_user_id_to_none(self):
        uid = self.make(b24_user_id=5)
        users.update_user(self.con, uid, b24_user_id=None)
        self.assertIsNone(users.get_user(self.con, uid)["b24_user_id"])

    def test_last_admin_protected(self):
        a = self.make("boss", is_admin=True)
        with self.assertRaises(Conflict):
            users.update_user(self.con, a, active=False)
        with self.assertRaises(Conflict):
            users.update_user(self.con, a, is_admin=False)
        users.update_user(self.con, a, fio="Босс")  # прочие правки разрешены
        b = self.make("boss2", fio="Второй", is_admin=True)
        users.update_user(self.con, a, is_admin=False)
        with self.assertRaises(Conflict):
            users.update_user(self.con, b, active=False)
        users.update_user(self.con, a, is_admin=True)
        users.update_user(self.con, b, active=False)

    def test_inactive_admin_is_not_counted(self):
        a = self.make("boss", is_admin=True)
        b = self.make("boss2", fio="Второй", is_admin=True)
        users.update_user(self.con, b, active=False)
        with self.assertRaises(Conflict):
            users.update_user(self.con, a, is_admin=False)


if __name__ == "__main__":
    unittest.main()
