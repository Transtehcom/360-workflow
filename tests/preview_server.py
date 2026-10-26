"""Локальная проверка интерфейса на временной базе без Битрикс24 и реальных участников."""
import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.common import ROOT, connect  # noqa: E402
from app.server import App, make_server  # noqa: E402
from app.users import create_user  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args()
    with open(os.path.join(ROOT, "config.example.json"), encoding="utf-8") as f:
        config = json.load(f)
    config["bitrix24"]["webhook"] = ""
    config["server"].update(secure_cookie=False, open_browser=False, public_url=f"http://127.0.0.1:{args.port}")
    with tempfile.TemporaryDirectory(prefix="s360-preview-") as directory:
        path = os.path.join(directory, "preview.sqlite")
        con = connect(path)
        create_user(con, "Администратор проверки", "preview-admin", "preview360", is_admin=True)
        create_user(con, "Тестовый сотрудник", "preview-user", "preview360", position="Специалист")
        app = App(config, con)
        httpd = make_server(app, "127.0.0.1", args.port)
        print(f"Проверка: http://127.0.0.1:{args.port}", flush=True)
        print("Логины preview-admin / preview-user, пароль preview360", flush=True)
        print(f"Временная база: {path}", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
            con.close()


if __name__ == "__main__":
    main()
