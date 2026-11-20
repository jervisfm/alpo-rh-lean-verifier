#!/usr/bin/env python3
"""
Automated native verifier for the Lean 4 Riemann Hypothesis / GRH proof claim
published at https://alpo.ge/remark.pdf.

Compatible with stock Ubuntu 20.04 LTS (Python 3.8+, zero external dependencies).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ---------------------------------------------------------------------------
# Constants & Ground-Truth Metadata
# ---------------------------------------------------------------------------

BASE_URL = "https://alpo.ge"
PART_COUNT = 14
PART_NAMES = [f"lean.tar.gz.part{i:02d}" for i in range(1, PART_COUNT + 1)]

# Known sizes of the 2026-11-20 alpo.ge archive parts (used as fallback if HEAD omits Content-Length)
KNOWN_PART_SIZES: Dict[str, int] = {
    f"lean.tar.gz.part{i:02d}": (5275554 if i == 14 else 18000000)
    for i in range(1, PART_COUNT + 1)
}
KNOWN_TOTAL_ARCHIVE_BYTES = sum(KNOWN_PART_SIZES.values())

# Exact theorems claimed in src/Main.lean
REQUIRED_THEOREMS: List[str] = [
    "riemannHypothesis",
    "riemannZeta_eq_zero_re",
    "LFunction_ne_zero_of_half_lt_re",
    "LFunction_eq_zero_re",
    "LFunction_eq_zero_re_of_isPrimitive",
]

# Standard axioms accepted in Lean 4 + Mathlib classical mathematics
ALLOWED_AXIOMS: Set[str] = {
    "propext",
    "Classical.choice",
    "Quot.sound",
}

# Prebuilt / binary extensions that must NOT exist inside the raw source tarball
FORBIDDEN_PREBUILT_EXTS: Set[str] = {
    ".olean",
    ".ilean",
    ".c",
    ".o",
    ".so",
    ".a",
    ".dylib",
    ".dll",
}

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
LAKE_PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]\s*(.*)")
CACHE_PROGRESS_RE = re.compile(
    r"(?:Downloading|Downloaded|Decompressing|Decompressed|Fetched|Unpacking)[^\d]*(\d+)\s*/\s*(\d+)",
    re.IGNORECASE,
)
AXIOMS_DEPENDS_RE = re.compile(
    r"'([^']+)'\s+depends on axioms:\s*\[([^\]]*)\]"
)
AXIOMS_NONE_RE = re.compile(
    r"'([^']+)'\s+does not depend on any axioms"
)


# ---------------------------------------------------------------------------
# Formatting & Progress / ETA Engine
# ---------------------------------------------------------------------------

def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0 or seconds != seconds or seconds == float("inf"):
        return "--:--:--"
    total = int(round(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def format_bytes(num_bytes: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    val = float(max(0.0, num_bytes))
    for unit in units:
        if val < 1024.0 or unit == units[-1]:
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} TiB"


class EtaEstimator:
    """
    Smoothed throughput and ETA estimator.

    Supports excluding an initial baseline (e.g. already-cached Lake modules
    replayed instantly at startup) so ETA accurately reflects newly compiled
    work in the current session.
    """

    def __init__(self, alpha: float = 0.20, min_sample_dt: float = 0.5) -> None:
        self.alpha = alpha
        self.min_sample_dt = min_sample_dt
        self.start_time = time.monotonic()
        self.work_start_time: Optional[float] = None
        self.baseline = 0.0
        self.last_value = 0.0
        self.last_time = self.start_time
        self.ewma_rate: Optional[float] = None

    def set_baseline(self, baseline: float) -> None:
        now = time.monotonic()
        self.baseline = max(self.baseline, float(baseline))
        self.last_value = max(self.last_value, self.baseline)
        self.last_time = now
        self.work_start_time = now

    def update(self, current: float, now: Optional[float] = None) -> None:
        if now is None:
            now = time.monotonic()
        current = float(current)
        if self.work_start_time is None:
            self.work_start_time = now
            self.last_time = now
            self.last_value = current
            return

        delta_val = current - self.last_value
        delta_t = now - self.last_time
        if delta_val > 0 and delta_t >= self.min_sample_dt:
            inst_rate = delta_val / delta_t
            if self.ewma_rate is None:
                self.ewma_rate = inst_rate
            else:
                self.ewma_rate = (self.alpha * inst_rate) + ((1.0 - self.alpha) * self.ewma_rate)
            self.last_value = current
            self.last_time = now

    def elapsed(self, now: Optional[float] = None) -> float:
        if now is None:
            now = time.monotonic()
        return max(0.0, now - self.start_time)

    def rate(self, current: float, now: Optional[float] = None) -> Optional[float]:
        if now is None:
            now = time.monotonic()
        work_done = max(0.0, float(current) - self.baseline)
        if work_done <= 0:
            return self.ewma_rate
        base_t = self.work_start_time if self.work_start_time is not None else self.start_time
        work_elapsed = max(0.001, now - base_t)
        overall_rate = work_done / work_elapsed
        if self.ewma_rate is None:
            return overall_rate
        # Blend overall session rate (stable) with EWMA (responsive to phase shifts)
        return 0.5 * overall_rate + 0.5 * self.ewma_rate

    def eta(self, current: float, total: float, now: Optional[float] = None) -> Optional[float]:
        if total <= 0:
            return None
        remaining = max(0.0, float(total) - float(current))
        if remaining == 0.0:
            return 0.0
        r = self.rate(current, now=now)
        if r is None or r <= 1e-9:
            return None
        return remaining / r


class ProgressBar:
    """Flicker-free terminal progress bar with ETA, plus clean non-TTY logging."""

    def __init__(
        self,
        label: str,
        total: float,
        unit: str = "items",
        is_bytes: bool = False,
        stream=None,
        log_interval_sec: float = 15.0,
    ) -> None:
        self.label = label
        self.total = float(total)
        self.unit = unit
        self.is_bytes = is_bytes
        self.stream = stream if stream is not None else sys.stdout
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.log_interval_sec = log_interval_sec
        self.estimator = EtaEstimator()
        self.current = 0.0
        self.detail = ""
        self._last_render = 0.0
        self._last_non_tty_pct = -10.0
        self._closed = False
        self._lock = threading.Lock()

    def set_total(self, total: float) -> None:
        with self._lock:
            self.total = max(float(total), self.current)

    def set_baseline(self, baseline: float) -> None:
        with self._lock:
            self.estimator.set_baseline(baseline)

    def update(self, current: float, detail: str = "", force: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            self.current = max(0.0, float(current))
            if self.total > 0 and self.current > self.total:
                self.total = self.current
            if detail:
                self.detail = detail
            self.estimator.update(self.current, now=now)

            if self.is_tty:
                if not force and (now - self._last_render) < 0.15:
                    return
                self._render_tty(now)
                self._last_render = now
            else:
                pct = (100.0 * self.current / self.total) if self.total > 0 else 0.0
                if (
                    force
                    or (now - self._last_render) >= self.log_interval_sec
                    or (pct - self._last_non_tty_pct) >= 5.0
                ):
                    self._render_non_tty(now)
                    self._last_render = now
                    self._last_non_tty_pct = pct

    def _format_line(self, now: float, bar_width: int = 24, max_cols: Optional[int] = None) -> str:
        pct = (100.0 * self.current / self.total) if self.total > 0 else 0.0
        pct = min(100.0, max(0.0, pct))

        if self.is_bytes:
            count_str = f"{format_bytes(self.current)}/{format_bytes(self.total)}"
            r = self.estimator.rate(self.current, now=now)
            rate_str = f"{format_bytes(r)}/s" if r is not None else "--/s"
        else:
            count_str = f"{int(self.current):,}/{int(self.total):,} {self.unit}"
            r = self.estimator.rate(self.current, now=now)
            rate_str = f"{r:.2f} {self.unit}/s" if r is not None else f"-- {self.unit}/s"

        elapsed_str = format_duration(self.estimator.elapsed(now=now))
        eta_str = format_duration(self.estimator.eta(self.current, self.total, now=now))

        # Adapt bar width on narrow terminals so percentage, count, rate, and ETA always remain visible
        if max_cols is not None:
            core_without_bar = (
                f"{self.label} [] {pct:5.1f}% | {count_str} | {rate_str} | "
                f"ETA {eta_str}"
            )
            with_elapsed = f"{core_without_bar} | {elapsed_str}"
            avail_for_bar = max_cols - len(with_elapsed) - 2
            if avail_for_bar >= 10:
                bar_width = min(bar_width, avail_for_bar)
                include_elapsed = True
            else:
                avail_for_bar = max_cols - len(core_without_bar) - 2
                bar_width = max(6, min(bar_width, avail_for_bar))
                include_elapsed = False
        else:
            include_elapsed = True

        filled = int(round(bar_width * pct / 100.0))
        bar = "█" * filled + "░" * (bar_width - filled)

        if include_elapsed:
            line = (
                f"{self.label} [{bar}] {pct:5.1f}% | {count_str} | "
                f"{rate_str} | {elapsed_str} | ETA {eta_str}"
            )
        else:
            line = (
                f"{self.label} [{bar}] {pct:5.1f}% | {count_str} | "
                f"{rate_str} | ETA {eta_str}"
            )

        if self.detail:
            if max_cols is None:
                line += f" | {self.detail}"
            else:
                remaining_cols = max_cols - len(line) - 4
                if remaining_cols >= 8:
                    det = self.detail
                    if len(det) > remaining_cols:
                        det = det[: remaining_cols - 3] + "..."
                    line += f" | {det}"
        return line

    def _render_tty(self, now: float) -> None:
        cols = shutil.get_terminal_size((120, 24)).columns
        line = self._format_line(now, bar_width=24, max_cols=cols)
        if len(line) >= cols:
            line = line[: max(10, cols - 4)] + "..."
        self.stream.write(f"\r\x1b[2K{line}")
        self.stream.flush()

    def _render_non_tty(self, now: float) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = self._format_line(now, bar_width=20)
        self.stream.write(f"[{ts}] {line}\n")
        self.stream.flush()

    def finish(self, detail: str = "done") -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            now = time.monotonic()
            if detail:
                self.detail = detail
            if self.is_tty:
                self._render_tty(now)
                self.stream.write("\n")
                self.stream.flush()
            else:
                self._render_non_tty(now)


class Spinner:
    """Progress indicator with elapsed time for indeterminate steps."""

    FRAMES = ["|", "/", "-", "\\"]

    def __init__(self, label: str, stream=None) -> None:
        self.label = label
        self.stream = stream if stream is not None else sys.stdout
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.start_time = time.monotonic()
        self.detail = ""
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._idx = 0
        self._thread: Optional[threading.Thread] = None

    def set_detail(self, detail: str) -> None:
        with self._lock:
            self.detail = detail.strip()

    def start(self) -> None:
        if not self.is_tty:
            ts = datetime.now().strftime("%H:%M:%S")
            self.stream.write(f"[{ts}] {self.label} ...\n")
            self.stream.flush()
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(0.2):
            with self._lock:
                frame = self.FRAMES[self._idx % len(self.FRAMES)]
                self._idx += 1
                elapsed = format_duration(time.monotonic() - self.start_time)
                cols = shutil.get_terminal_size((120, 24)).columns
                line = f"{self.label} [{frame}] Elapsed {elapsed}"
                if self.detail:
                    line += f" | {self.detail}"
                if len(line) >= cols:
                    line = line[: max(10, cols - 4)] + "..."
                self.stream.write(f"\r\x1b[2K{line}")
                self.stream.flush()

    def finish(self, status: str = "done") -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        elapsed = format_duration(time.monotonic() - self.start_time)
        if self.is_tty:
            self.stream.write(f"\r\x1b[2K{self.label} [done] Elapsed {elapsed} | {status}\n")
        else:
            ts = datetime.now().strftime("%H:%M:%S")
            self.stream.write(f"[{ts}] {self.label} finished in {elapsed}: {status}\n")
        self.stream.flush()


# ---------------------------------------------------------------------------
# System Resource & Nice/Ionice Helpers
# ---------------------------------------------------------------------------

def detect_system_resources(work_dir: Path) -> Dict[str, object]:
    cpus = os.cpu_count() or 1

    ram_total_bytes = 0
    ram_avail_bytes = 0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    ram_total_bytes = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    ram_avail_bytes = int(line.split()[1]) * 1024
    except OSError:
        pass

    os_pretty = "Linux"
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PRETTY_NAME="):
                    os_pretty = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass

    check_path = work_dir if work_dir.exists() else work_dir.parent
    while not check_path.exists() and check_path != check_path.parent:
        check_path = check_path.parent
    disk_free_bytes = shutil.disk_usage(str(check_path)).free

    return {
        "os": os_pretty,
        "cpus": cpus,
        "ram_total_bytes": ram_total_bytes,
        "ram_avail_bytes": ram_avail_bytes,
        "disk_free_bytes": disk_free_bytes,
    }


def compute_nice_default_jobs(cpus: int, ram_total_bytes: int) -> int:
    """
    Pick a resource-friendly default LEAN_NUM_THREADS:
    - Always leave at least 1 CPU core free when cpus > 1.
    - Budget ~3.5 GiB of RAM per concurrent Lean worker to avoid OOM kills.
    """
    cpu_cap = max(1, cpus - 1) if cpus > 1 else 1
    if ram_total_bytes > 0:
        ram_gib = ram_total_bytes / (1024 ** 3)
        ram_cap = max(1, int(ram_gib // 3.5))
        return max(1, min(cpu_cap, ram_cap))
    return cpu_cap


def probe_ionice_prefix(mode: str = "auto") -> List[str]:
    """
    Determine a working ionice command prefix.
    mode='idle' tries -c 3; mode='auto' tries -c 3 then -c 2 -n 7; mode='off' disables.
    """
    if mode == "off":
        return []
    ionice_bin = shutil.which("ionice")
    if not ionice_bin:
        return []

    candidates: List[List[str]] = []
    if mode in ("auto", "idle"):
        candidates.append([ionice_bin, "-c", "3"])
    if mode in ("auto", "best-effort"):
        candidates.append([ionice_bin, "-c", "2", "-n", "7"])

    for prefix in candidates:
        try:
            res = subprocess.run(
                prefix + ["true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if res.returncode == 0:
                return prefix
        except OSError:
            continue
    return []


def build_nice_cmd(
    cmd: Sequence[str],
    nice_level: int = 19,
    ionice_prefix: Optional[Sequence[str]] = None,
) -> List[str]:
    prefix: List[str] = []
    if ionice_prefix:
        prefix.extend(ionice_prefix)
    nice_bin = shutil.which("nice")
    if nice_bin and nice_level is not None:
        prefix.extend([nice_bin, "-n", str(int(nice_level))])
    prefix.extend(cmd)
    return prefix


def raise_nofile_limit(target: int = 65536) -> Tuple[int, int]:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    desired = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if desired > soft:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
            return desired, hard
        except (OSError, ValueError):
            return soft, hard
    return soft, hard


# ---------------------------------------------------------------------------
# Parsing & Auditing Functions (Unit-Tested)
# ---------------------------------------------------------------------------

def load_checksums(path: Path) -> Dict[str, str]:
    checksums: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                digest = parts[0].lower()
                filename = Path(parts[1]).name
                checksums[filename] = digest
    return checksums


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest().lower()


def parse_lake_line(raw_line: str) -> Dict[str, object]:
    """
    Parse a single line of `lake build --no-ansi` output.

    Returns a dict with keys:
      - done: Optional[int]
      - total: Optional[int]
      - text: str (cleaned)
      - is_error: bool
      - is_sorry_warning: bool
      - is_cache_hit: bool
      - is_compiled: bool
    """
    clean = strip_ansi(raw_line).strip()
    done: Optional[int] = None
    total: Optional[int] = None
    body = clean

    m = LAKE_PROGRESS_RE.search(clean)
    if m:
        done = int(m.group(1))
        total = int(m.group(2))
        body = m.group(3).strip()

    lower = clean.lower()
    is_error = (
        clean.startswith("✖")
        or clean.startswith("error:")
        or " error: " in lower
        or lower.startswith("build failed")
    )
    is_sorry_warning = "declaration uses 'sorry'" in lower
    is_cache_hit = (
        body.startswith("Replayed ")
        or body.startswith("Fetched ")
        or body.startswith("Restored ")
    )
    is_compiled = body.startswith("Built ")

    return {
        "done": done,
        "total": total,
        "text": body,
        "raw": clean,
        "is_error": is_error,
        "is_sorry_warning": is_sorry_warning,
        "is_cache_hit": is_cache_hit,
        "is_compiled": is_compiled,
    }


def parse_print_axioms(
    output: str,
    required_theorems: Sequence[str] = REQUIRED_THEOREMS,
    allowed_axioms: Set[str] = ALLOWED_AXIOMS,
) -> Tuple[bool, Dict[str, List[str]], List[str]]:
    """
    Parse `#print axioms` output from `lake env lean scripts/audit_axioms.lean`.

    Returns:
      (passed, theorem_to_axioms, violations)
    """
    found: Dict[str, List[str]] = {}
    for raw_line in output.splitlines():
        line = strip_ansi(raw_line).strip()
        m_dep = AXIOMS_DEPENDS_RE.search(line)
        if m_dep:
            name = m_dep.group(1).strip()
            if name.startswith("_root_."):
                name = name[len("_root_."):]
            raw_axioms = m_dep.group(2).strip()
            axioms = [a.strip() for a in raw_axioms.split(",") if a.strip()]
            found[name] = axioms
            continue
        m_none = AXIOMS_NONE_RE.search(line)
        if m_none:
            name = m_none.group(1).strip()
            if name.startswith("_root_."):
                name = name[len("_root_."):]
            found[name] = []

    violations: List[str] = []
    for thm in required_theorems:
        if thm not in found:
            violations.append(f"Missing '#print axioms' output for required theorem '{thm}'")
            continue
        disallowed = [ax for ax in found[thm] if ax not in allowed_axioms]
        if disallowed:
            violations.append(
                f"Theorem '{thm}' depends on disallowed axiom(s): {', '.join(disallowed)}"
            )

    return (len(violations) == 0), found, violations


def _strip_lean_comments(source: str) -> str:
    """Remove Lean block (`/- ... -/`) and line (`-- ...`) comments conservatively."""
    out: List[str] = []
    i = 0
    n = len(source)
    depth = 0
    while i < n:
        if depth == 0 and source[i : i + 2] == "--":
            nl = source.find("\n", i)
            if nl == -1:
                break
            out.append("\n")
            i = nl + 1
            continue
        if source[i : i + 2] == "/-":
            depth += 1
            i += 2
            continue
        if depth > 0 and source[i : i + 2] == "-/":
            depth -= 1
            i += 2
            continue
        if depth == 0:
            out.append(source[i])
        i += 1
    return "".join(out)


def audit_source_tree(lean_dir: Path) -> Tuple[bool, List[str], Dict[str, object]]:
    """
    Verify extracted source tree before compilation:
    1. Required project files exist.
    2. Zero prebuilt `.olean`/binary artifacts were smuggled in the tarball.
    3. `src/Statements.lean` and `src/Main.lean` contain no suspicious keywords
       (axiom, opaque, sorry, admit, implemented_by, extern, native_decide).
    """
    errors: List[str] = []
    required_files = [
        "lean-toolchain",
        "lakefile.toml",
        "lake-manifest.json",
        "src/Statements.lean",
        "src/Main.lean",
    ]
    for rel in required_files:
        if not (lean_dir / rel).is_file():
            errors.append(f"Missing required file in archive: {rel}")

    lean_file_count = 0
    smuggled: List[str] = []
    for root, dirs, files in os.walk(str(lean_dir)):
        # Skip .lake directory if resuming a prior build; check only outside .lake
        rel_root = Path(root).relative_to(lean_dir)
        if rel_root.parts and rel_root.parts[0] == ".lake":
            dirs[:] = []
            continue
        for fname in files:
            ext = Path(fname).suffix.lower()
            if ext == ".lean":
                lean_file_count += 1
            elif ext in FORBIDDEN_PREBUILT_EXTS:
                smuggled.append(str(rel_root / fname))

    if smuggled:
        errors.append(
            f"Found {len(smuggled)} forbidden prebuilt artifact(s) in source tree "
            f"(e.g. {', '.join(smuggled[:5])})"
        )

    suspicious_re = re.compile(
        r"\b(axiom|opaque|sorry|admit|implemented_by|native_decide)\b|@\[[^\]]*\bextern\b"
    )
    for rel in ("src/Statements.lean", "src/Main.lean"):
        p = lean_dir / rel
        if p.is_file():
            cleaned = _strip_lean_comments(p.read_text(encoding="utf-8", errors="replace"))
            hits = suspicious_re.findall(cleaned)
            if hits:
                errors.append(f"Suspicious token(s) found in {rel}: {', '.join(sorted(set(hits)))}")

    stats = {
        "lean_file_count": lean_file_count,
        "smuggled_artifacts": len(smuggled),
    }
    return (len(errors) == 0), errors, stats


# ---------------------------------------------------------------------------
# Pipeline Stages
# ---------------------------------------------------------------------------

def download_part(
    url: str,
    dest: Path,
    progress_callback,
) -> None:
    """Download a single part via native curl with resume and retries (Ubuntu 20.04 curl 7.68+ safe)."""
    partial = dest.with_suffix(dest.suffix + ".part")
    cmd = [
        "curl",
        "--proto", "=https",
        "--tlsv1.2",
        "-fsSL",
        "--retry", "5",
        "--retry-delay", "2",
        "--connect-timeout", "30",
        "-C", "-",
        "-o", str(partial),
        url,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert proc.stderr is not None

    while proc.poll() is None:
        if partial.exists():
            try:
                progress_callback(partial.stat().st_size)
            except OSError:
                pass
        time.sleep(0.15)

    stderr_text = proc.stderr.read().decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"curl failed for {url} (exit {proc.returncode}): {stderr_text}")

    if partial.exists():
        progress_callback(partial.stat().st_size)
        partial.replace(dest)


def stage_download(
    downloads_dir: Path,
    base_url: str,
    expected_checksums: Dict[str, str],
    download_jobs: int,
    allow_upstream_change: bool,
) -> Tuple[bool, str, List[Path]]:
    downloads_dir.mkdir(parents=True, exist_ok=True)
    part_paths = [downloads_dir / name for name in PART_NAMES]

    sizes_lock = threading.Lock()
    downloaded_bytes: Dict[str, int] = {}
    to_download: List[Tuple[str, Path]] = []

    for name, path in zip(PART_NAMES, part_paths):
        if path.is_file() and path.stat().st_size > 0:
            exp = expected_checksums.get(name)
            if exp and sha256_file(path) == exp:
                downloaded_bytes[name] = path.stat().st_size
                continue
            if allow_upstream_change and path.stat().st_size == KNOWN_PART_SIZES.get(name, -1):
                downloaded_bytes[name] = path.stat().st_size
                continue
        downloaded_bytes[name] = 0
        to_download.append((name, path))

    if not to_download:
        print(f"[Stage 1/7] All {PART_COUNT} archive parts already downloaded and verified.")
        return True, f"All {PART_COUNT} parts cached locally", part_paths

    bar = ProgressBar(
        label="[Stage 1/7] Download",
        total=KNOWN_TOTAL_ARCHIVE_BYTES,
        is_bytes=True,
    )
    already = sum(downloaded_bytes.values())
    if already > 0:
        bar.set_baseline(already)
        bar.update(already, detail=f"{PART_COUNT - len(to_download)}/{PART_COUNT} cached", force=True)

    completed_parts = PART_COUNT - len(to_download)

    def _run_one(item: Tuple[str, Path]) -> str:
        name, dest = item
        url = f"{base_url.rstrip('/')}/{name}"

        def _cb(nbytes: int) -> None:
            with sizes_lock:
                downloaded_bytes[name] = nbytes
                total_now = sum(downloaded_bytes.values())
            bar.update(total_now, detail=f"{completed_parts}/{PART_COUNT} parts | {name}")

        download_part(url, dest, _cb)
        return name

    errors: List[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, download_jobs)) as pool:
        futs = {pool.submit(_run_one, item): item[0] for item in to_download}
        for fut in concurrent.futures.as_completed(futs):
            name = futs[fut]
            try:
                fut.result()
                completed_parts += 1
                with sizes_lock:
                    total_now = sum(downloaded_bytes.values())
                bar.update(total_now, detail=f"{completed_parts}/{PART_COUNT} parts", force=True)
            except Exception as exc:
                errors.append(f"{name}: {exc}")

    if errors:
        bar.finish("FAILED")
        return False, "; ".join(errors), part_paths

    final_total = sum(p.stat().st_size for p in part_paths)
    bar.set_total(final_total)
    bar.update(final_total, detail=f"{PART_COUNT}/{PART_COUNT} parts", force=True)
    bar.finish("complete")
    return True, f"Downloaded {PART_COUNT} parts ({format_bytes(final_total)})", part_paths


def stage_verify_checksums(
    part_paths: Sequence[Path],
    expected_checksums: Dict[str, str],
    allow_upstream_change: bool,
) -> Tuple[bool, str, Dict[str, str]]:
    total_bytes = sum(p.stat().st_size for p in part_paths)
    bar = ProgressBar(label="[Stage 2/7] SHA-256 ", total=total_bytes, is_bytes=True)

    actual: Dict[str, str] = {}
    mismatches: List[str] = []
    done_bytes = 0

    for idx, p in enumerate(part_paths, start=1):
        digest = sha256_file(p)
        actual[p.name] = digest
        done_bytes += p.stat().st_size
        exp = expected_checksums.get(p.name)
        if exp and digest != exp:
            mismatches.append(f"{p.name}: expected {exp[:12]}..., got {digest[:12]}...")
        bar.update(done_bytes, detail=f"{idx}/{len(part_paths)} {p.name}")

    if mismatches and not allow_upstream_change:
        bar.finish("MISMATCH")
        msg = (
            "Archive SHA-256 mismatch (rerun with --allow-upstream-change if alpo.ge updated the archive): "
            + "; ".join(mismatches[:3])
        )
        return False, msg, actual

    if mismatches and allow_upstream_change:
        bar.finish("upstream changed (allowed)")
        return True, f"SHA-256 differs from 2026-11-20 snapshot ({len(mismatches)} parts; allowed)", actual

    bar.finish("all 14 SHA-256 hashes verified")
    return True, "All 14 archive parts match pinned SHA-256 checksums", actual


def stage_extract_and_audit(
    part_paths: Sequence[Path],
    extract_root: Path,
    nice_level: int,
    ionice_prefix: Sequence[str],
    force_clean: bool,
) -> Tuple[bool, str, Path, Dict[str, object]]:
    lean_dir = extract_root / "lean"
    sentinel = extract_root / ".extracted.ok"

    if force_clean and extract_root.exists():
        print(f"[Stage 3/7] Cleaning previous extraction at {extract_root} ...")
        shutil.rmtree(extract_root)

    if sentinel.is_file() and (lean_dir / "lakefile.toml").is_file():
        print(f"[Stage 3/7] Reusing extracted source tree at {lean_dir} (use --clean to re-extract).")
    else:
        extract_root.mkdir(parents=True, exist_ok=True)
        total_bytes = sum(p.stat().st_size for p in part_paths)
        decompressor = shutil.which("pigz") or shutil.which("gzip")
        if not decompressor:
            return False, "Neither pigz nor gzip found on PATH", lean_dir, {}

        decomp_name = Path(decompressor).name
        bar = ProgressBar(
            label=f"[Stage 3/7] Extract ({decomp_name})",
            total=total_bytes,
            is_bytes=True,
        )

        decomp_cmd = build_nice_cmd([decompressor, "-dc"], nice_level, ionice_prefix)
        tar_cmd = build_nice_cmd(["tar", "-xf", "-", "-C", str(extract_root)], nice_level, ionice_prefix)

        p_decomp = subprocess.Popen(
            decomp_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        p_tar = subprocess.Popen(
            tar_cmd,
            stdin=p_decomp.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        assert p_decomp.stdin is not None
        assert p_decomp.stdout is not None
        p_decomp.stdout.close()

        streamed = 0
        chunk_size = 512 * 1024
        try:
            for idx, part in enumerate(part_paths, start=1):
                with open(part, "rb") as fh:
                    while True:
                        buf = fh.read(chunk_size)
                        if not buf:
                            break
                        p_decomp.stdin.write(buf)
                        streamed += len(buf)
                        bar.update(streamed, detail=f"part {idx}/{len(part_paths)}")
            p_decomp.stdin.close()
        except BrokenPipeError:
            pass

        tar_err = p_tar.stderr.read().decode("utf-8", errors="replace").strip() if p_tar.stderr else ""
        decomp_err = p_decomp.stderr.read().decode("utf-8", errors="replace").strip() if p_decomp.stderr else ""
        rc_tar = p_tar.wait()
        rc_decomp = p_decomp.wait()

        if rc_decomp != 0 or rc_tar != 0:
            bar.finish("FAILED")
            return False, f"Extraction failed (decomp={rc_decomp}: {decomp_err}; tar={rc_tar}: {tar_err})", lean_dir, {}

        bar.update(total_bytes, detail="extracted", force=True)
        bar.finish("complete")
        sentinel.write_text(datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8")

    # Source-only & static sanity audit (before any build!)
    ok, audit_errors, stats = audit_source_tree(lean_dir)
    if not ok:
        return False, "; ".join(audit_errors), lean_dir, stats

    return (
        True,
        f"Source tree verified ({stats['lean_file_count']:,} .lean files, 0 prebuilt artifacts)",
        lean_dir,
        stats,
    )


def stage_toolchain_and_mathlib_cache(
    lean_dir: Path,
    logs_dir: Path,
    nice_level: int,
    ionice_prefix: Sequence[str],
    jobs: int,
    use_mathlib_cache: bool,
    require_mathlib_cache: bool,
) -> Tuple[bool, str, str]:
    toolchain = (lean_dir / "lean-toolchain").read_text(encoding="utf-8").strip()
    if not toolchain:
        return False, "lean-toolchain file is empty", ""

    env = os.environ.copy()
    env["LEAN_NUM_THREADS"] = str(jobs)
    elan_bin = Path(env.get("ELAN_HOME", str(Path.home() / ".elan"))) / "bin"
    if elan_bin.is_dir():
        env["PATH"] = f"{elan_bin}:{env.get('PATH', '')}"

    # 1. Ensure exact pinned Lean toolchain is installed natively via elan
    spinner = Spinner(f"[Stage 4/7] Lean toolchain ({toolchain})")
    spinner.start()
    tc_log = logs_dir / "elan-toolchain.log"
    with open(tc_log, "w", encoding="utf-8") as log_fh:
        cmd = build_nice_cmd(["elan", "toolchain", "install", toolchain], nice_level, ionice_prefix)
        proc = subprocess.Popen(
            cmd,
            cwd=str(lean_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            log_fh.write(line)
            clean = strip_ansi(line).strip()
            if clean:
                spinner.set_detail(clean)
        rc = proc.wait()

    if rc != 0:
        spinner.finish("FAILED")
        return False, f"elan failed to install {toolchain} (see {tc_log})", toolchain

    # Verify native lean binary executes on this host (catches glibc issues early)
    ver_res = subprocess.run(
        ["lean", "--version"],
        cwd=str(lean_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        check=False,
    )
    if ver_res.returncode != 0:
        spinner.finish("FAILED")
        return (
            False,
            f"Native lean binary failed to execute ({ver_res.stdout.strip()}). "
            "Check glibc/toolchain compatibility.",
            toolchain,
        )
    lean_ver_line = ver_res.stdout.strip().splitlines()[0] if ver_res.stdout.strip() else toolchain
    spinner.finish(lean_ver_line)

    # 2. Fetch prebuilt Mathlib .olean cache for fast native execution
    if not use_mathlib_cache:
        print("[Stage 4/7] Skipping Mathlib cache (--no-mathlib-cache); Mathlib will compile natively.")
        return True, f"{lean_ver_line} (Mathlib cache skipped)", toolchain

    cache_log = logs_dir / "mathlib-cache.log"
    cache_spinner = Spinner("[Stage 4/7] Mathlib cache (lake exe cache get)")
    cache_spinner.start()
    cache_bar: Optional[ProgressBar] = None

    cmd = build_nice_cmd(["lake", "exe", "cache", "get"], nice_level, ionice_prefix)
    with open(cache_log, "w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(lean_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            log_fh.write(line)
            clean = strip_ansi(line).strip()
            if not clean:
                continue
            m = CACHE_PROGRESS_RE.search(clean)
            if m:
                done, total = int(m.group(1)), int(m.group(2))
                if total > 0:
                    if cache_bar is None:
                        cache_spinner.finish("downloading")
                        cache_bar = ProgressBar(
                            label="[Stage 4/7] Mathlib cache",
                            total=total,
                            unit="files",
                        )
                    cache_bar.set_total(total)
                    cache_bar.update(done, detail=clean[:40])
            else:
                if cache_bar is None:
                    cache_spinner.set_detail(clean)
        rc = proc.wait()

    if cache_bar is not None:
        cache_bar.finish("complete" if rc == 0 else "fallback")
    else:
        cache_spinner.finish("complete" if rc == 0 else "fallback")

    if rc != 0:
        if require_mathlib_cache:
            return False, f"lake exe cache get failed (exit {rc}; see {cache_log})", toolchain
        print(
            f"[Stage 4/7] WARNING: Mathlib cache fetch exited {rc} (see {cache_log}); "
            "falling back to native compilation."
        )
        return True, f"{lean_ver_line} (cache fallback to native build)", toolchain

    return True, f"{lean_ver_line} + Mathlib cache ready", toolchain


def stage_lake_build(
    lean_dir: Path,
    logs_dir: Path,
    nice_level: int,
    ionice_prefix: Sequence[str],
    jobs: int,
    all_targets: bool,
) -> Tuple[bool, str, Dict[str, object]]:
    target_args = [] if all_targets else ["Main"]
    target_desc = "all default targets" if all_targets else "Main (RH + GRH transitive closure)"

    env = os.environ.copy()
    env["LEAN_NUM_THREADS"] = str(jobs)
    elan_bin = Path(env.get("ELAN_HOME", str(Path.home() / ".elan"))) / "bin"
    if elan_bin.is_dir():
        env["PATH"] = f"{elan_bin}:{env.get('PATH', '')}"

    cmd = build_nice_cmd(
        ["lake", "build", "--no-ansi"] + target_args,
        nice_level,
        ionice_prefix,
    )
    build_log = logs_dir / "lake-build.log"
    print(f"[Stage 5/7] Compiling {target_desc} with LEAN_NUM_THREADS={jobs}, nice={nice_level} ...")

    bar: Optional[ProgressBar] = None
    spinner = Spinner("[Stage 5/7] Planning Lean build graph")
    spinner.start()

    max_done = 0
    total_jobs = 0
    compiled_count = 0
    cached_count = 0
    sorry_warnings: List[str] = []
    error_lines: List[str] = []
    first_compile_seen = False

    start_t = time.monotonic()
    with open(build_log, "w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(lean_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        assert proc.stdout is not None
        try:
            for raw_line in proc.stdout:
                log_fh.write(raw_line)
                log_fh.flush()
                parsed = parse_lake_line(raw_line)

                if parsed["is_sorry_warning"]:
                    sorry_warnings.append(str(parsed["raw"]))
                if parsed["is_error"] and len(error_lines) < 50:
                    error_lines.append(str(parsed["raw"]))

                done = parsed["done"]
                total = parsed["total"]
                if isinstance(done, int) and isinstance(total, int) and total > 0:
                    if bar is None:
                        spinner.finish(f"{total:,} build jobs")
                        bar = ProgressBar(
                            label="[Stage 5/7] Lean verify",
                            total=total,
                            unit="jobs",
                        )
                    total_jobs = max(total_jobs, total)
                    bar.set_total(total_jobs)

                    if parsed["is_cache_hit"]:
                        cached_count += 1
                        if not first_compile_seen:
                            bar.set_baseline(done)
                    elif parsed["is_compiled"]:
                        if not first_compile_seen:
                            first_compile_seen = True
                            bar.set_baseline(max(0, done - 1))
                        compiled_count += 1

                    max_done = max(max_done, done)
                    detail = str(parsed["text"])
                    if len(detail) > 48:
                        detail = detail[:45] + "..."
                    bar.update(max_done, detail=detail)
                else:
                    raw_clean = str(parsed["raw"])
                    if raw_clean and bar is None:
                        spinner.set_detail(raw_clean)
            rc = proc.wait()
        except KeyboardInterrupt:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                proc.terminate()
            proc.wait(timeout=5)
            raise

    elapsed = time.monotonic() - start_t
    if bar is not None:
        if rc == 0 and total_jobs > 0:
            bar.update(total_jobs, detail="complete", force=True)
        bar.finish("complete" if rc == 0 else f"FAILED (exit {rc})")
    else:
        spinner.finish("complete" if rc == 0 else f"FAILED (exit {rc})")

    stats = {
        "exit_code": rc,
        "total_jobs": total_jobs,
        "completed_jobs": max_done,
        "compiled_this_run": compiled_count,
        "cached_hits": cached_count,
        "sorry_warnings": len(sorry_warnings),
        "elapsed_seconds": round(elapsed, 2),
        "log_file": str(build_log),
    }

    if rc != 0:
        sample = "; ".join(error_lines[:3]) if error_lines else f"see {build_log}"
        return False, f"lake build failed (exit {rc}): {sample}", stats

    if sorry_warnings:
        return (
            False,
            f"lake build emitted {len(sorry_warnings)} 'sorry' warning(s) (see {build_log})",
            stats,
        )

    return (
        True,
        f"lake build succeeded ({total_jobs:,} jobs, {compiled_count:,} compiled, "
        f"{cached_count:,} cached) in {format_duration(elapsed)}",
        stats,
    )


def stage_axiom_and_statement_audit(
    lean_dir: Path,
    audit_script_src: Path,
    work_dir: Path,
    logs_dir: Path,
    nice_level: int,
    ionice_prefix: Sequence[str],
    jobs: int,
) -> Tuple[bool, str, Dict[str, List[str]]]:
    audit_file = work_dir / "audit_axioms.lean"
    shutil.copy2(audit_script_src, audit_file)

    env = os.environ.copy()
    env["LEAN_NUM_THREADS"] = str(jobs)
    elan_bin = Path(env.get("ELAN_HOME", str(Path.home() / ".elan"))) / "bin"
    if elan_bin.is_dir():
        env["PATH"] = f"{elan_bin}:{env.get('PATH', '')}"

    spinner = Spinner("[Stage 6/7] Axiom & statement audit")
    spinner.start()

    cmd = build_nice_cmd(["lake", "env", "lean", str(audit_file)], nice_level, ionice_prefix)
    res = subprocess.run(
        cmd,
        cwd=str(lean_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        check=False,
    )
    audit_log = logs_dir / "axiom-audit.log"
    audit_log.write_text(res.stdout, encoding="utf-8")

    if res.returncode != 0:
        spinner.finish("FAILED")
        first_err = next(
            (ln.strip() for ln in res.stdout.splitlines() if "error:" in ln.lower()),
            f"exit {res.returncode} (see {audit_log})",
        )
        return False, f"Statement/axiom audit failed to typecheck: {first_err}", {}

    if "declaration uses 'sorry'" in res.stdout.lower():
        spinner.finish("FAILED (sorry)")
        return False, f"Audit output reported 'sorry' usage (see {audit_log})", {}

    passed, thm_axioms, violations = parse_print_axioms(res.stdout)
    if not passed:
        spinner.finish("FAILED (disallowed axioms)")
        return False, "; ".join(violations), thm_axioms

    spinner.finish("all 5 theorems match Mathlib statements & use only standard axioms")
    return True, "All 5 theorems match canonical Mathlib statements using only standard axioms", thm_axioms


def stage_leanchecker(
    lean_dir: Path,
    logs_dir: Path,
    nice_level: int,
    ionice_prefix: Sequence[str],
    jobs: int,
    skip: bool,
) -> Tuple[Optional[bool], str]:
    if skip:
        print("[Stage 7/7] Skipping independent kernel replay (--skip-leanchecker).")
        return None, "Skipped by --skip-leanchecker"

    env = os.environ.copy()
    env["LEAN_NUM_THREADS"] = str(jobs)
    elan_bin = Path(env.get("ELAN_HOME", str(Path.home() / ".elan"))) / "bin"
    if elan_bin.is_dir():
        env["PATH"] = f"{elan_bin}:{env.get('PATH', '')}"

    # Check if leanchecker is available in this toolchain
    probe = subprocess.run(
        ["lake", "env", "which", "leanchecker"],
        cwd=str(lean_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        print("[Stage 7/7] WARNING: leanchecker binary not found in toolchain; skipping kernel replay.")
        return None, "leanchecker not present in toolchain"

    spinner = Spinner("[Stage 7/7] Independent kernel replay (leanchecker --fresh Main)")
    spinner.start()

    cmd = build_nice_cmd(
        ["lake", "env", "leanchecker", "--fresh", "Main"],
        nice_level,
        ionice_prefix,
    )
    checker_log = logs_dir / "leanchecker.log"
    start_t = time.monotonic()
    with open(checker_log, "w", encoding="utf-8") as log_fh:
        proc = subprocess.Popen(
            cmd,
            cwd=str(lean_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        assert proc.stdout is not None
        last_line = ""
        for line in proc.stdout:
            log_fh.write(line)
            clean = strip_ansi(line).strip()
            if clean:
                last_line = clean
                spinner.set_detail(clean)
        rc = proc.wait()

    elapsed = time.monotonic() - start_t
    if rc != 0:
        spinner.finish("FAILED")
        return False, f"leanchecker failed (exit {rc}): {last_line or checker_log}"

    spinner.finish(f"passed in {format_duration(elapsed)}")
    return True, f"Kernel replay of Main passed in {format_duration(elapsed)}"


# ---------------------------------------------------------------------------
# Report & CLI
# ---------------------------------------------------------------------------

def render_verdict_banner(
    succeeded: bool,
    gates: List[Dict[str, object]],
    theorem_axioms: Dict[str, List[str]],
    total_elapsed: float,
    report_txt_path: Path,
    report_json_path: Path,
) -> str:
    status_str = "VERIFICATION SUCCEEDED" if succeeded else "VERIFICATION FAILED"
    lines = [
        "",
        "=" * 74,
        f"  {status_str}",
        "=" * 74,
        f"  Claim source : https://alpo.ge/remark.pdf (lean.tar.gz.part01..part14)",
        f"  Total time   : {format_duration(total_elapsed)}",
        f"  Finished UTC : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "-" * 74,
        "  Verification Gates:",
    ]
    for g in gates:
        state = g["status"]
        lines.append(f"    [{state:7s}] {g['name']}: {g['detail']}")

    if theorem_axioms:
        lines.append("-" * 74)
        lines.append("  Audited Theorems & Axioms:")
        for thm in REQUIRED_THEOREMS:
            if thm in theorem_axioms:
                ax_list = ", ".join(theorem_axioms[thm]) if theorem_axioms[thm] else "(none)"
                lines.append(f"    - {thm}: [{ax_list}]")

    lines.extend(
        [
            "-" * 74,
            f"  Text report  : {report_txt_path}",
            f"  JSON report  : {report_json_path}",
            "=" * 74,
            "",
        ]
    )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verify.sh",
        description=(
            "Automatically verify the Lean 4 Riemann Hypothesis / GRH proof claim "
            "linked from https://alpo.ge/remark.pdf natively on Ubuntu 20.04+."
        ),
    )
    p.add_argument(
        "--work-dir",
        type=Path,
        default=Path.cwd() / "work",
        help="Working directory for downloads, extracted Lean project, logs, and reports (default: ./work)",
    )
    p.add_argument(
        "--base-url",
        default=BASE_URL,
        help=f"Base URL hosting lean.tar.gz.part01..part14 (default: {BASE_URL})",
    )
    p.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=None,
        help="Concurrent Lean worker threads (LEAN_NUM_THREADS). Default: auto-tuned nicely from CPU & RAM.",
    )
    p.add_argument(
        "--download-jobs",
        type=int,
        default=4,
        help="Parallel archive download connections (default: 4).",
    )
    p.add_argument(
        "--nice",
        type=int,
        default=19,
        help="CPU nice level 0..19 for all verification subprocesses (default: 19, nicest).",
    )
    p.add_argument(
        "--ionice",
        choices=["auto", "idle", "best-effort", "off"],
        default="auto",
        help="I/O scheduling priority class (default: auto -> idle class 3, fallback best-effort 7).",
    )
    p.add_argument(
        "--all-targets",
        action="store_true",
        help="Build all 11 default targets in lakefile.toml instead of only Main's transitive closure.",
    )
    p.add_argument(
        "--no-mathlib-cache",
        action="store_true",
        help="Skip `lake exe cache get` and compile Mathlib from source natively.",
    )
    p.add_argument(
        "--require-mathlib-cache",
        action="store_true",
        help="Fail immediately if `lake exe cache get` fails instead of falling back to source build.",
    )
    p.add_argument(
        "--skip-leanchecker",
        action="store_true",
        help="Skip independent kernel replay (`lake env leanchecker --fresh Main`).",
    )
    p.add_argument(
        "--allow-upstream-change",
        action="store_true",
        help="Proceed with a warning if alpo.ge archive SHA-256 hashes differ from the 2026-11-20 snapshot.",
    )
    p.add_argument(
        "--clean",
        action="store_true",
        help="Delete previously extracted Lean project before re-extracting (keeps verified downloaded parts).",
    )
    p.add_argument(
        "--no-apt",
        action="store_true",
        help="Handled by verify.sh: skip automatic apt-get installation of missing system tools.",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    script_dir = Path(__file__).resolve().parent
    work_dir: Path = args.work_dir.resolve()
    downloads_dir = work_dir / "downloads"
    extract_root = work_dir / "src"
    logs_dir = work_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Be nice in the orchestrator process too
    nice_level = max(0, min(19, int(args.nice)))
    try:
        cur_nice = os.nice(0)
        if nice_level > cur_nice:
            os.nice(nice_level - cur_nice)
    except OSError:
        pass

    soft_nofile, _ = raise_nofile_limit(65536)
    ionice_prefix = probe_ionice_prefix(args.ionice)
    sys_info = detect_system_resources(work_dir)
    jobs = (
        max(1, int(args.jobs))
        if args.jobs is not None
        else compute_nice_default_jobs(int(sys_info["cpus"]), int(sys_info["ram_total_bytes"]))
    )

    ram_gib = int(sys_info["ram_total_bytes"]) / (1024 ** 3) if sys_info["ram_total_bytes"] else 0.0
    disk_gib = int(sys_info["disk_free_bytes"]) / (1024 ** 3)

    print("=" * 74)
    print("  Automated Lean 4 Verifier — https://alpo.ge/remark.pdf (RH & GRH)")
    print("=" * 74)
    print(f"  Host OS        : {sys_info['os']}")
    print(f"  CPU / RAM      : {sys_info['cpus']} cores / {ram_gib:.1f} GiB RAM")
    print(f"  Free disk      : {disk_gib:.1f} GiB at {work_dir}")
    print(f"  Resource mode  : nice={nice_level}, ionice={' '.join(ionice_prefix) or 'none'}, LEAN_NUM_THREADS={jobs}, ulimit -n={soft_nofile}")
    print("=" * 74)

    if ram_gib > 0 and ram_gib < 8.0:
        print("  WARNING: Less than 8 GiB RAM detected; large Lean/Mathlib modules may require swap.")
    if disk_gib < 25.0:
        print("  WARNING: Less than 25 GiB free disk space; building 175k+ modules may require 25-40 GiB.")

    expected_checksums = load_checksums(script_dir / "checksums.sha256")
    audit_script_src = script_dir / "scripts" / "audit_axioms.lean"

    overall_start = time.monotonic()
    gates: List[Dict[str, object]] = []
    theorem_axioms: Dict[str, List[str]] = {}
    actual_checksums: Dict[str, str] = {}
    build_stats: Dict[str, object] = {}
    extract_stats: Dict[str, object] = {}
    toolchain_str = ""
    succeeded = False

    try:
        # Stage 1: Download
        ok, detail, part_paths = stage_download(
            downloads_dir=downloads_dir,
            base_url=args.base_url,
            expected_checksums=expected_checksums,
            download_jobs=args.download_jobs,
            allow_upstream_change=args.allow_upstream_change,
        )
        gates.append({"name": "1. Archive download", "status": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            raise SystemExit(1)

        # Stage 2: SHA-256 verification
        ok, detail, actual_checksums = stage_verify_checksums(
            part_paths=part_paths,
            expected_checksums=expected_checksums,
            allow_upstream_change=args.allow_upstream_change,
        )
        gates.append({"name": "2. Archive SHA-256", "status": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            raise SystemExit(1)

        # Stage 3: Extract & source-only audit
        ok, detail, lean_dir, extract_stats = stage_extract_and_audit(
            part_paths=part_paths,
            extract_root=extract_root,
            nice_level=nice_level,
            ionice_prefix=ionice_prefix,
            force_clean=args.clean,
        )
        gates.append({"name": "3. Extract & source audit", "status": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            raise SystemExit(1)

        # Stage 4: Native Lean toolchain & Mathlib cache
        ok, detail, toolchain_str = stage_toolchain_and_mathlib_cache(
            lean_dir=lean_dir,
            logs_dir=logs_dir,
            nice_level=nice_level,
            ionice_prefix=ionice_prefix,
            jobs=jobs,
            use_mathlib_cache=not args.no_mathlib_cache,
            require_mathlib_cache=args.require_mathlib_cache,
        )
        gates.append({"name": "4. Toolchain & Mathlib cache", "status": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            raise SystemExit(1)

        # Stage 5: Proof compilation (`lake build`)
        ok, detail, build_stats = stage_lake_build(
            lean_dir=lean_dir,
            logs_dir=logs_dir,
            nice_level=nice_level,
            ionice_prefix=ionice_prefix,
            jobs=jobs,
            all_targets=args.all_targets,
        )
        gates.append({"name": "5. Lean proof build", "status": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            raise SystemExit(1)

        # Stage 6: Theorem statement & axiom soundness audit
        ok, detail, theorem_axioms = stage_axiom_and_statement_audit(
            lean_dir=lean_dir,
            audit_script_src=audit_script_src,
            work_dir=work_dir,
            logs_dir=logs_dir,
            nice_level=nice_level,
            ionice_prefix=ionice_prefix,
            jobs=jobs,
        )
        gates.append({"name": "6. Statement & axiom audit", "status": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            raise SystemExit(1)

        # Stage 7: Independent kernel replay (leanchecker)
        chk_ok, detail = stage_leanchecker(
            lean_dir=lean_dir,
            logs_dir=logs_dir,
            nice_level=nice_level,
            ionice_prefix=ionice_prefix,
            jobs=jobs,
            skip=args.skip_leanchecker,
        )
        chk_status = "SKIPPED" if chk_ok is None else ("PASS" if chk_ok else "FAIL")
        gates.append({"name": "7. Kernel replay (leanchecker)", "status": chk_status, "detail": detail})
        if chk_ok is False:
            raise SystemExit(1)

        succeeded = True

    except KeyboardInterrupt:
        print("\n[Interrupted] Received Ctrl-C. Progress is preserved; re-run ./verify.sh to resume.")
        gates.append({"name": "Execution", "status": "FAIL", "detail": "Interrupted by user (SIGINT)"})
        succeeded = False
    except SystemExit:
        succeeded = False
    except Exception as exc:
        gates.append({"name": "Unexpected error", "status": "FAIL", "detail": str(exc)})
        succeeded = False

    total_elapsed = time.monotonic() - overall_start
    report_txt_path = work_dir / "verification-report.txt"
    report_json_path = work_dir / "verification-report.json"

    banner = render_verdict_banner(
        succeeded=succeeded,
        gates=gates,
        theorem_axioms=theorem_axioms,
        total_elapsed=total_elapsed,
        report_txt_path=report_txt_path,
        report_json_path=report_json_path,
    )
    print(banner)

    report_txt_path.write_text(banner.strip() + "\n", encoding="utf-8")
    report_payload = {
        "verdict": "SUCCEEDED" if succeeded else "FAILED",
        "succeeded": succeeded,
        "claim_url": "https://alpo.ge/remark.pdf",
        "archive_base_url": args.base_url,
        "toolchain": toolchain_str,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "total_elapsed_seconds": round(total_elapsed, 2),
        "system": sys_info,
        "settings": {
            "jobs": jobs,
            "nice": nice_level,
            "ionice": ionice_prefix,
            "all_targets": args.all_targets,
            "use_mathlib_cache": not args.no_mathlib_cache,
            "skip_leanchecker": args.skip_leanchecker,
        },
        "gates": gates,
        "theorems": theorem_axioms,
        "extract_stats": extract_stats,
        "build_stats": build_stats,
        "archive_sha256": actual_checksums,
    }
    report_json_path.write_text(json.dumps(report_payload, indent=2) + "\n", encoding="utf-8")

    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
