import Mathlib
import Main

/-!
Independent statement & axiom audit for the Riemann Hypothesis / GRH
formalization linked from https://alpo.ge/remark.pdf.

This file verifies two things via the Lean 4 kernel:
1. The five exported theorems in `Main` are definitionally typed against
   Mathlib's canonical `_root_.RiemannHypothesis`, `_root_.riemannZeta`,
   and `_root_.DirichletCharacter.LFunction` definitions (preventing
   definition shadowing or weakened statements).
2. Prints the exact transitive axiom dependencies of each theorem so the
   verification runner can assert that only standard Lean axioms
   (`propext`, `Classical.choice`, `Quot.sound`) are used.
-/

-- 1. Verify exact statement types against Mathlib's canonical definitions
example : _root_.RiemannHypothesis :=
  _root_.riemannHypothesis

example :
    ∀ s : ℂ, _root_.riemannZeta s = 0 →
      (¬∃ n : ℕ, s = -2 * ((n : ℂ) + 1)) → s ≠ 1 → s.re = 1 / 2 :=
  _root_.riemannHypothesis

example :
    ∀ s : ℂ, _root_.riemannZeta s = 0 →
      (¬∃ n : ℕ, s = -2 * ((n : ℂ) + 1)) → s ≠ 1 → s.re = 1 / 2 :=
  _root_.riemannZeta_eq_zero_re

example :
    ∀ (N : ℕ) [NeZero N] (χ : _root_.DirichletCharacter ℂ N) (s : ℂ),
      1 / 2 < s.re → (χ ≠ 1 ∨ s ≠ 1) → _root_.DirichletCharacter.LFunction χ s ≠ 0 :=
  _root_.LFunction_ne_zero_of_half_lt_re

example :
    ∀ (N : ℕ) [NeZero N] (χ : _root_.DirichletCharacter ℂ N) (s : ℂ),
      0 < s.re → s.re < 1 → _root_.DirichletCharacter.LFunction χ s = 0 → s.re = 1 / 2 :=
  _root_.LFunction_eq_zero_re

example :
    ∀ (N : ℕ) [NeZero N] (χ : _root_.DirichletCharacter ℂ N), χ.IsPrimitive →
      ∀ s : ℂ, 0 < s.re → s.re < 1 → _root_.DirichletCharacter.LFunction χ s = 0 → s.re = 1 / 2 :=
  _root_.LFunction_eq_zero_re_of_isPrimitive

-- 2. Emit exact kernel-collected axiom sets for all 5 main theorems
#print axioms _root_.riemannHypothesis
#print axioms _root_.riemannZeta_eq_zero_re
#print axioms _root_.LFunction_ne_zero_of_half_lt_re
#print axioms _root_.LFunction_eq_zero_re
#print axioms _root_.LFunction_eq_zero_re_of_isPrimitive
