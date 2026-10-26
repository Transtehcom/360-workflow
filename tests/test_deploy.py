import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: Path) -> str:
    # Часть файлов сохранена с BOM.
    return path.read_text(encoding="utf-8-sig")


class DeployFilesTest(unittest.TestCase):
    def test_no_kpi_leftovers(self):
        files = [p for p in (ROOT / "deploy").rglob("*") if p.is_file()]
        files += [ROOT / "run.bat", ROOT / "start.ps1"]
        self.assertGreater(len(files), 2)
        for path in files:
            text = read(path)
            for bad in ("kpi-dashboard", "18081", "8081"):
                self.assertNotIn(bad, text, f"{path.name}: остаток «{bad}»")

    def test_install_does_not_overwrite_caddyfile(self):
        text = read(ROOT / "deploy" / "install.sh")
        self.assertRegex(text, r"(?m)^.*>> /etc/caddy/Caddyfile")
        self.assertNotRegex(text, r"(?m)^[^#\n]*(?<!>)> /etc/caddy/Caddyfile")

    def test_autostart_ports(self):
        text = read(ROOT / "deploy" / "windows" / "office-autostart.ps1")
        self.assertIn("RemotePort = 18082", text)

    def test_autostart_keeps_previous_app_logs(self):
        # Start-Process перезаписывает журналы: перед каждым запуском прежние уходят в *.prev.log
        text = read(ROOT / "deploy" / "windows" / "office-autostart.ps1")
        start_app = re.search(r"(?s)function Start-App \{(.*?)\n    \}", text).group(1)
        self.assertIn('"app.out", "app.err"', start_app)
        self.assertIn('"data\\$name.prev.log"', start_app)
        self.assertLess(start_app.index("Move-Item"), start_app.index("Start-Process -FilePath"))

    def run_install_config(self, config):
        """Python-часть install.sh, которая правит config.json, — на временной копии."""
        script = re.search(r"(?s)<<'PY'\n(.*?)\nPY\n", read(ROOT / "deploy" / "install.sh")).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            out = subprocess.run([sys.executable, "-I", "-c", script, str(path), "360.transtehcom.ru", "443"],
                                 capture_output=True, text=True, check=True)
            self.assertEqual(out.stdout.strip(), "8082")
            return json.loads(path.read_text(encoding="utf-8"))

    def test_install_sets_office_timezone_only_when_empty(self):
        # офис — UTC+5: на сервере в UTC «сегодня», сроки и напоминания иначе сдвинулись бы
        self.assertEqual(self.run_install_config({"timezone": ""})["timezone"], "Asia/Yekaterinburg")
        self.assertEqual(self.run_install_config({})["timezone"], "Asia/Yekaterinburg")
        self.assertEqual(self.run_install_config({"timezone": "Europe/Moscow"})["timezone"], "Europe/Moscow")
        server = self.run_install_config({"timezone": ""})["server"]
        self.assertEqual(server["public_url"], "https://360.transtehcom.ru")
        self.assertTrue(server["secure_cookie"])


if __name__ == "__main__":
    unittest.main()
