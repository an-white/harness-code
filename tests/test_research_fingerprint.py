import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.research.fingerprint import compute_fingerprint


def _settings():
    return SimpleNamespace(research_max_files=40, research_max_bytes=200000)


class ResearchFingerprintTest(unittest.TestCase):
    def test_stable_and_changes_on_relevant_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("v1\n", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname='a'\n", encoding="utf-8")
            fp1 = compute_fingerprint(root, settings=_settings())["fingerprint"]
            fp2 = compute_fingerprint(root, settings=_settings())["fingerprint"]
            self.assertEqual(fp1, fp2)
            self.assertTrue(fp1)

            (root / "README.md").write_text("v2\n", encoding="utf-8")
            fp3 = compute_fingerprint(root, settings=_settings())["fingerprint"]
            self.assertNotEqual(fp1, fp3)

    def test_ignores_noise_and_secrets_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("base\n", encoding="utf-8")
            base = compute_fingerprint(root, settings=_settings())["fingerprint"]
            # excluded/noise should not affect fingerprint
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
            (root / ".venv").mkdir()
            (root / ".venv" / "x.txt").write_text("v", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "y.js").write_text("v", encoding="utf-8")
            fp2 = compute_fingerprint(root, settings=_settings())["fingerprint"]
            self.assertEqual(base, fp2)

    def test_symlinked_files_outside_not_read_or_hashed(self):
        with tempfile.TemporaryDirectory() as ws, tempfile.TemporaryDirectory() as outside:
            root = Path(ws)
            out = Path(outside)
            (root / "README.md").write_text("base\n", encoding="utf-8")
            base_info = compute_fingerprint(root, settings=_settings())
            base = base_info["fingerprint"]

            secret = out / "secret.txt"
            secret.write_text("OUTSIDE_LEAK_FP", encoding="utf-8")
            (root / "linked.txt").symlink_to(secret)
            (root / "pyproject.toml").symlink_to(secret)
            (root / "leakdir").symlink_to(out, target_is_directory=True)

            info2 = compute_fingerprint(root, settings=_settings())
            blob = str(info2)
            self.assertNotIn("OUTSIDE_LEAK_FP", blob)
            # symlinked outside content must not be incorporated
            self.assertEqual(base, info2["fingerprint"])
            self.assertNotIn("pyproject.toml", info2.get("manifests", []))


if __name__ == "__main__":
    unittest.main()
