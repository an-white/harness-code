import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.research.scanner import scan_workspace


def _settings(**overrides):
    base = {"research_max_files": 30, "research_max_bytes": 50000}
    base.update(overrides)
    return SimpleNamespace(**base)


class ResearchScannerTest(unittest.TestCase):
    def test_bounded_deterministic_inventory_and_excludes_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
            (root / "README.md").write_text("# Demo\nThis project uses HTTP API and JWT acronyms.\n", encoding="utf-8")
            (root / "app").mkdir()
            (root / "app" / "main.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")
            (root / ".github" / "workflows").mkdir(parents=True)
            (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
            # noise/secrets to exclude
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
            (root / "id.pem").write_text("pem", encoding="utf-8")
            (root / ".venv" / "lib").mkdir(parents=True)
            (root / ".venv" / "lib" / "x.py").write_text("x=1", encoding="utf-8")
            (root / "node_modules" / "pkg").mkdir(parents=True)
            (root / "node_modules" / "pkg" / "i.js").write_text("x", encoding="utf-8")
            (root / "dist").mkdir()
            (root / "dist" / "out.txt").write_text("built", encoding="utf-8")

            scan1 = scan_workspace(root, settings=_settings())
            scan2 = scan_workspace(root, settings=_settings())
            self.assertEqual(scan1["files_sample"], scan2["files_sample"])
            self.assertEqual(scan1["manifests"], scan2["manifests"])
            joined = "\n".join(scan1["files_sample"] + scan1.get("directories", []))
            self.assertNotIn(".env", joined)
            self.assertNotIn("id.pem", joined)
            self.assertNotIn(".venv", joined)
            self.assertNotIn("node_modules", joined)
            self.assertNotIn("dist/out.txt", joined)

            self.assertTrue(any(m["path"] == "pyproject.toml" for m in scan1["manifests"]))
            self.assertIn("app/main.py", scan1["entry_points"])
            self.assertIn(".github/workflows", scan1["ci_configs"])
            self.assertTrue(any(r["path"] == "README.md" for r in scan1["readmes"]))
            self.assertIn("HTTP", scan1["heuristics"].get("acronyms", []))
            self.assertIsInstance(scan1.get("recent_git_log"), list)
            self.assertEqual(scan1["repos"], ["."] if (root / ".git").exists() else [])

    def test_bounded_limits_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(80):
                (root / f"f{i}.txt").write_text("x" * 50, encoding="utf-8")
            scan = scan_workspace(root, settings=_settings(research_max_files=10, research_max_bytes=2000))
            self.assertLessEqual(len(scan["files_sample"]), 10)
            self.assertEqual(scan["limits"]["max_files"], 10)

    def test_symlinked_candidates_not_read_or_leaked(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as outside:
            root = Path(ws)
            out = Path(outside)
            secret = out / "secret.txt"
            secret.write_text("LEAK_TOKEN_OUTSIDE_12345", encoding="utf-8")
            (out / "README_secret.md").write_text("LEAK_README_OUTSIDE", encoding="utf-8")
            # symlinked file in tree and as candidates
            (root / "linked.txt").symlink_to(secret)
            (root / "README.md").symlink_to(out / "README_secret.md")
            (root / "pyproject.toml").symlink_to(secret)
            (root / "app").mkdir()
            (root / "app" / "main.py").symlink_to(secret)
            (root / "leakdir").symlink_to(out, target_is_directory=True)

            scan = scan_workspace(root, settings=_settings())
            blob = str(scan)
            self.assertNotIn("LEAK_TOKEN_OUTSIDE_12345", blob)
            self.assertNotIn("LEAK_README_OUTSIDE", blob)
            joined = "\n".join(scan.get("files_sample", []) + scan.get("directories", []))
            self.assertNotIn("linked.txt", joined)
            self.assertNotIn("leakdir", joined)
            self.assertFalse(any(m.get("path") == "pyproject.toml" for m in scan.get("manifests", [])))
            self.assertFalse(any(r.get("path") == "README.md" for r in scan.get("readmes", [])))
            self.assertNotIn("app/main.py", scan.get("entry_points", []))


if __name__ == "__main__":
    unittest.main()
