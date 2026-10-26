import re
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


if __name__ == "__main__":
    unittest.main()
