# Automated Lean 4 Verifier for `alpo.ge/remark.pdf` (RH & GRH Claim)

A native, resource-friendly automated verification harness for the Lean 4 formalization of the Riemann Hypothesis (and Generalized Riemann Hypothesis) linked in footnote 1 of Levent Alpöge's note at [`https://alpo.ge/remark.pdf`](https://alpo.ge/remark.pdf).

Designed to start cleanly on a fresh **Ubuntu 20.04 LTS (Focal Fossa)** system with zero Docker/VM overhead and zero third-party Python dependencies.

## Quick Start (Clean Ubuntu 20.04 LTS)

```bash
git clone https://github.com/jervisfm/alpo-rh-lean-verifier.git
cd alpo-rh-lean-verifier
./verify.sh
```

To run in the background (e.g. over SSH or inside `tmux`/`screen`) with clean timestamped logs:

```bash
./verify.sh 2>&1 | tee verify.log
```

The verification is **fully resumable**: if interrupted with `Ctrl-C` or a reboot, re-running `./verify.sh` reuses already-verified archive parts, the extracted tree, the Mathlib `.olean` cache, and all previously compiled `.olean` build outputs.

---

## What the Script Verifies

The script runs **7 sequential verification gates** and prints a final **`VERIFICATION SUCCEEDED`** or **`VERIFICATION FAILED`** banner (plus machine-readable reports in `work/verification-report.txt` and `work/verification-report.json`):

1. **Archive Download (14 parts, ~228 MiB compressed)**
   - Downloads `https://alpo.ge/lean.tar.gz.part01` through `part14` in parallel using native `curl` (with resume & retries, compatible with Ubuntu 20.04's `curl 7.68`).
   - Displays a real-time byte-level progress bar, throughput (`MiB/s`), elapsed time, and ETA.
2. **Cryptographic Integrity Check (`checksums.sha256`)**
   - Verifies the SHA-256 hash of every part against the pinned 2026-11-20 snapshot in [`checksums.sha256`](checksums.sha256).
   - Pass `--allow-upstream-change` if `alpo.ge` later publishes an updated archive.
3. **Native Streaming Extraction & Source-Only Audit**
   - Streams `part01..part14` directly into native `pigz` (parallel gzip, if installed) or `gzip` piped into `tar` — avoiding duplicate intermediate archives — with a byte-level progress bar and ETA.
   - Audits the extracted source tree (`175,187` `.lean` files, ~1.4 GiB) to confirm **zero prebuilt `.olean`/binary artifacts** were smuggled in, and checks `src/Statements.lean` and `src/Main.lean` for forbidden tokens (`axiom`, `opaque`, `sorry`, `admit`, `implemented_by`, `@[extern]`, `native_decide`).
4. **Native Lean 4 Toolchain & Mathlib Cache**
   - Installs `elan` natively (statically linked binary, runs natively on Ubuntu 20.04 `glibc 2.31`) and installs the exact pinned toolchain from `lean-toolchain` (`leanprover/lean4:v4.33.1`, compiled for `glibc 2.26+`).
   - Fetches Mathlib's official precompiled `.olean` cache (`lake exe cache get`) for fast native execution (use `--no-mathlib-cache` to compile Mathlib from scratch).
5. **Proof Verification (`lake build Main`) with Progress Bar & ETA**
   - Compiles `Main` (the complete transitive dependency closure of all 5 RH & GRH theorems in `src/Main.lean`), or all 11 targets with `--all-targets`.
   - Parses Lake's `[done/total]` job counters in real time to render a flicker-free progress bar (`█░`), module throughput (`jobs/s`), elapsed time, and a smoothed EWMA ETA that separates instant cache hits from freshly compiled modules.
   - Fails immediately if `lake build` returns non-zero or emits any `declaration uses 'sorry'` warning.
6. **Independent Statement & Axiom Soundness Audit (`scripts/audit_axioms.lean`)**
   - Runs an external Lean file (`lake env lean`) that imports `Mathlib` and `Main` and asserts exact definitional type matches against Mathlib's canonical `_root_.RiemannHypothesis`, `_root_.riemannZeta`, and `_root_.DirichletCharacter.LFunction` definitions for all 5 theorems:
     - `riemannHypothesis`
     - `riemannZeta_eq_zero_re`
     - `LFunction_ne_zero_of_half_lt_re`
     - `LFunction_eq_zero_re`
     - `LFunction_eq_zero_re_of_isPrimitive`
   - Runs `#print axioms` on all 5 theorems and **fails verification** if any axiom outside the 3 standard classical Lean axioms (`propext`, `Classical.choice`, `Quot.sound`) is present (explicitly rejecting `sorryAx`, `Lean.ofReduceBool`, `Lean.trustCompiler`, or custom axioms).
7. **Independent Kernel Replay (`lake env leanchecker --fresh Main`)**
   - Replays the compiled `.olean` environment of `Main` through a fresh Lean 4 kernel to rule out metaprogramming/tactic environment injection.

---

## Resource Friendliness (`nice`, `ionice`, RAM & FD Limits)

Compiling 175,000+ Lean 4 modules with Mathlib is compute- and memory-intensive. By default, `./verify.sh` is designed to keep your machine responsive:

- **CPU priority**: Runs all build, decompression, and verification steps under `nice -n 19` (configurable via `--nice N`).
- **I/O priority**: Uses `ionice -c 3` (idle I/O class, automatically falling back to `ionice -c 2 -n 7` or none if restricted by the kernel/container).
- **Memory-aware parallelism**: Automatically sets `LEAN_NUM_THREADS` to `min(max(1, cores - 1), max(1, floor(RAM_GiB / 3.5)))` so Lean workers do not starve interactive tasks or trigger the OOM killer (override with `--jobs N`).
- **Ubuntu 20.04 file descriptor limit**: Automatically raises `ulimit -n` (soft limit) toward `65536` (capped at hard limit) so Ubuntu 20.04's default `1024` limit does not cause `EMFILE` errors.

### Recommended Machine Specs

| Resource | Minimum | Recommended |
|---|---|---|
| OS | Ubuntu 20.04 LTS (x86_64 or aarch64) | Ubuntu 20.04 / 22.04 / 24.04 LTS |
| RAM | 8 GiB (+ swap, `--jobs 1` or `2`) | 32+ GiB |
| Free Disk | 25 GiB | 50+ GiB (SSD/NVMe strongly recommended) |

---

## CLI Options

```text
./verify.sh [OPTIONS]

  --work-dir DIR            Working directory for downloads, source, logs, reports (default: ./work)
  --jobs N, -j N            Concurrent Lean worker threads (LEAN_NUM_THREADS); default auto-tuned
  --download-jobs N         Parallel archive download connections (default: 4)
  --nice N                  CPU nice level 0..19 (default: 19)
  --ionice MODE             I/O priority: auto | idle | best-effort | off (default: auto)
  --all-targets             Build all 11 lakefile.toml targets instead of only Main
  --no-mathlib-cache        Compile Mathlib from source instead of fetching prebuilt .oleans
  --require-mathlib-cache   Fail if Mathlib cache download fails (no source-build fallback)
  --skip-leanchecker        Skip Stage 7 independent kernel replay
  --allow-upstream-change   Warn instead of failing if alpo.ge archive checksums change
  --clean                   Re-extract the Lean archive from scratch (keeps downloaded parts)
  --no-apt                  Do not invoke apt-get to install missing system packages
  -h, --help                Show help message and exit
```

## Example Final Output

```text
==========================================================================
  VERIFICATION SUCCEEDED
==========================================================================
  Claim source : https://alpo.ge/remark.pdf (lean.tar.gz.part01..part14)
  Total time   : 04:12:38
  Finished UTC : 2026-11-20 22:15:04 UTC
--------------------------------------------------------------------------
  Verification Gates:
    [PASS   ] 1. Archive download: Downloaded 14 parts (228.2 MiB)
    [PASS   ] 2. Archive SHA-256: All 14 archive parts match pinned SHA-256 checksums
    [PASS   ] 3. Extract & source audit: Source tree verified (175,187 .lean files, 0 prebuilt artifacts)
    [PASS   ] 4. Toolchain & Mathlib cache: Lean (version 4.33.1, ...) + Mathlib cache ready
    [PASS   ] 5. Lean proof build: lake build succeeded (...)
    [PASS   ] 6. Statement & axiom audit: All 5 theorems match canonical Mathlib statements using only standard axioms
    [PASS   ] 7. Kernel replay (leanchecker): Kernel replay of Main passed (...)
--------------------------------------------------------------------------
  Audited Theorems & Axioms:
    - riemannHypothesis: [propext, Classical.choice, Quot.sound]
    - riemannZeta_eq_zero_re: [propext, Classical.choice, Quot.sound]
    - LFunction_ne_zero_of_half_lt_re: [propext, Classical.choice, Quot.sound]
    - LFunction_eq_zero_re: [propext, Classical.choice, Quot.sound]
    - LFunction_eq_zero_re_of_isPrimitive: [propext, Classical.choice, Quot.sound]
==========================================================================
```

Exit code is `0` if verification succeeded and `1` if verification failed.

## Running the Verifier's Own Unit Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT License — see [LICENSE](LICENSE).
