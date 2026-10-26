"""Управление приложением «Оценка 360°».

  python manage.py run                 — запустить веб-сервер
  python manage.py create-admin ЛОГИН  — создать администратора (ФИО и пароль спросит)
  python manage.py password ЛОГИН      — задать пароль пользователю
"""
import getpass
import sys

sys.stdout.reconfigure(encoding="utf-8")


def ask_password():
    pwd = getpass.getpass("Пароль: ")
    if pwd != getpass.getpass("Пароль ещё раз: "):
        sys.exit("Пароли не совпадают")
    if not pwd:
        sys.exit("Пароль не может быть пустым")
    return pwd


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        from app.server import serve
        serve()
    elif cmd == "create-admin":
        from app import users
        from app.common import BadRequest, Conflict, connect, db_lock
        login = sys.argv[2] if len(sys.argv) > 2 else input("Логин: ")
        fio = input("ФИО: ")
        pwd = ask_password()
        try:
            with db_lock():
                users.create_user(connect(), fio, login, pwd, is_admin=True)
        except (BadRequest, Conflict) as e:
            sys.exit(str(e))
        print("Администратор создан")
    elif cmd == "password":
        from app import users
        from app.common import BadRequest, connect, db_lock
        con = connect()
        login = (sys.argv[2] if len(sys.argv) > 2 else input("Логин: ")).strip().lower()
        row = con.execute("SELECT id FROM users WHERE login=?", (login,)).fetchone()
        if not row:
            sys.exit(f"Пользователя {login} нет")
        pwd = ask_password()
        try:
            with db_lock():
                users.update_user(con, row["id"], password=pwd)
        except BadRequest as e:
            sys.exit(str(e))
        print("Пароль сохранён")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
