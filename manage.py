"""Управление приложением «Оценка 360°».

  python manage.py run                 — запустить веб-сервер
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        from app.server import serve
        serve()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
