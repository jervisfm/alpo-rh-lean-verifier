#!/usr/bin/env python3
"""Unit tests for verify_rh.py (compatible with Ubuntu 20.04 Python 3.8+)."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

import verify_rh as v


class TestFormattingAndEta(unittest.TestCase):
    def test_format_duration(self) -> None:
        self.assertEqual(v.format_duration(None), "--:--:--")
        self.assertEqual(v.format_duration(0), "00:00:00")
        self.assertEqual(v.format_duration(3661), "01:01:01")
        self.assertEqual(v.format_duration(90061), "1d 01:01:01")

    def test_format_bytes(self) -> None:
        self.assertEqual(v.format_bytes(512), "512 B")
        self.assertEqual(v.format_bytes(1024), "1.0 KiB")
        self.assertEqual(v.format_bytes(18 * 1024 * 1024), "18.0 MiB")

    def test_eta_excludes_baseline(self) -> None:
        est = v.EtaEstimator(alpha=0.5, min_sample_dt=0.5)
        t0 = 1000.0
        est.work_start_time = t0
        est.last_time = t0
        est.baseline = 100.0
        est.last_value = 100.0

        # 10 new items built in 10 seconds -> 1.0 item/sec; 90 remaining -> ~90s ETA
        est.update(110.0, now=t0 + 10.0)
        eta = est.eta(110.0, 200.0, now=t0 + 10.0)
        self.assertIsNotNone(eta)
        assert eta is not None
        self.assertAlmostEqual(eta, 90.0, places=2)

    def test_progress_bar_non_tty_output(self) -> None:
        buf = io.StringIO()
        bar = v.ProgressBar("Test", total=100, unit="mods", stream=buf, log_interval_sec=0.0)
        bar.update(50, detail="Main.lean", force=True)
        bar.finish("done")
        out = buf.getvalue()
        self.assertIn("50.0%", out)
        self.assertIn("50/100 mods", out)
        self.assertIn("ETA", out)


class TestLakeLineParser(unittest.TestCase):
    def test_compiled_module_line(self) -> None:
        parsed = v.parse_lake_line("\x1b[32m✔\x1b[0m [1234/175187] Built Main (1.4s)")
        self.assertEqual(parsed["done"], 1234)
        self.assertEqual(parsed["total"], 175187)
        self.assertTrue(parsed["is_compiled"])
        self.assertFalse(parsed["is_cache_hit"])
        self.assertFalse(parsed["is_error"])

    def test_replayed_cache_line(self) -> None:
        parsed = v.parse_lake_line("✔ [42/175187] Replayed Mathlib.Data.Nat.Basic")
        self.assertEqual(parsed["done"], 42)
        self.assertTrue(parsed["is_cache_hit"])
        self.assertFalse(parsed["is_compiled"])

    def test_sorry_and_error_detection(self) -> None:
        w = v.parse_lake_line("warning: Main.lean:4:8: declaration uses 'sorry'")
        self.assertTrue(w["is_sorry_warning"])
        e = v.parse_lake_line("✖ [99/100] Building Main")
        self.assertTrue(e["is_error"])


class TestAxiomAuditParser(unittest.TestCase):
    def test_standard_axioms_pass(self) -> None:
        sample = "\n".join([
            "'riemannHypothesis' depends on axioms: [propext, Classical.choice, Quot.sound]",
            "'riemannZeta_eq_zero_re' depends on axioms: [propext, Classical.choice, Quot.sound]",
            "'LFunction_ne_zero_of_half_lt_re' depends on axioms: [propext, Classical.choice, Quot.sound]",
            "'LFunction_eq_zero_re' depends on axioms: [propext, Classical.choice, Quot.sound]",
            "'LFunction_eq_zero_re_of_isPrimitive' does not depend on any axioms",
        ])
        ok, axioms, violations = v.parse_print_axioms(sample)
        self.assertTrue(ok, violations)
        self.assertEqual(axioms["riemannHypothesis"], ["propext", "Classical.choice", "Quot.sound"])
        self.assertEqual(axioms["LFunction_eq_zero_re_of_isPrimitive"], [])

    def test_rejects_sorry_and_trust_compiler_and_custom(self) -> None:
        sample = "\n".join([
            "'riemannHypothesis' depends on axioms: [propext, sorryAx]",
            "'riemannZeta_eq_zero_re' depends on axioms: [Lean.ofReduceBool]",
            "'LFunction_ne_zero_of_half_lt_re' depends on axioms: [Lean.trustCompiler]",
            "'LFunction_eq_zero_re' depends on axioms: [MyBogusAxiom]",
            "'LFunction_eq_zero_re_of_isPrimitive' depends on axioms: [propext]",
        ])
        ok, _, violations = v.parse_print_axioms(sample)
        self.assertFalse(ok)
        self.assertEqual(len(violations), 4)

    def test_rejects_missing_theorem(self) -> None:
        sample = "'riemannHypothesis' depends on axioms: [propext, Classical.choice, Quot.sound]"
        ok, _, violations = v.parse_print_axioms(sample)
        self.assertFalse(ok)
        self.assertEqual(len(violations), 4)


class TestSourceTreeAudit(unittest.TestCase):
    def _make_minimal_tree(self, root: Path) -> None:
        (root / "src").mkdir(parents=True)
        (root / "lean-toolchain").write_text("leanprover/lean4:v4.33.1\n", encoding="utf-8")
        (root / "lakefile.toml").write_text('name = "tree"\n', encoding="utf-8")
        (root / "lake-manifest.json").write_text("{}\n", encoding="utf-8")
        (root / "src" / "Statements.lean").write_text(
            "import Mathlib\n-- comment mentioning sorry axiom\ndef Foo : Prop := True\n",
            encoding="utf-8",
        )
        (root / "src" / "Main.lean").write_text(
            "import Statements\ntheorem riemannHypothesis : True := trivial\n",
            encoding="utf-8",
        )

    def test_clean_tree_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_minimal_tree(root)
            ok, errors, stats = v.audit_source_tree(root)
            self.assertTrue(ok, errors)
            self.assertEqual(stats["lean_file_count"], 2)
            self.assertEqual(stats["smuggled_artifacts"], 0)

    def test_smuggled_olean_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_minimal_tree(root)
            (root / "src" / "Main.olean").write_bytes(b"fake")
            ok, errors, stats = v.audit_source_tree(root)
            self.assertFalse(ok)
            self.assertEqual(stats["smuggled_artifacts"], 1)

    def test_axiom_in_main_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_minimal_tree(root)
            (root / "src" / "Main.lean").write_text(
                "axiom cheat : False\n", encoding="utf-8"
            )
            ok, errors, _ = v.audit_source_tree(root)
            self.assertFalse(ok)
            self.assertTrue(any("Main.lean" in e for e in errors))


class TestResourceHelpers(unittest.TestCase):
    def test_compute_nice_default_jobs(self) -> None:
        self.assertEqual(v.compute_nice_default_jobs(1, 16 * 1024**3), 1)
        self.assertEqual(v.compute_nice_default_jobs(4, 32 * 1024**3), 3)
        self.assertEqual(v.compute_nice_default_jobs(16, 8 * 1024**3), 2)
        self.assertEqual(v.compute_nice_default_jobs(16, 64 * 1024**3), 15)

    def test_build_nice_cmd(self) -> None:
        cmd = v.build_nice_cmd(["lake", "build"], nice_level=19, ionice_prefix=["/usr/bin/ionice", "-c", "3"])
        self.assertEqual(cmd[-2:], ["lake", "build"])
        self.assertIn("19", cmd)
        self.assertEqual(cmd[:3], ["/usr/bin/ionice", "-c", "3"])

    def test_checksums_file_has_all_14_parts(self) -> None:
        checksums_path = Path(__file__).resolve().parent.parent / "checksums.sha256"
        checksums = v.load_checksums(checksums_path)
        self.assertEqual(len(checksums), 14)
        for name in v.PART_NAMES:
            self.assertIn(name, checksums)
            self.assertEqual(len(checksums[name]), 64)


if __name__ == "__main__":
    unittest.main()
