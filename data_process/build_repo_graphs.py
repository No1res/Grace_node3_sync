#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import signal
import sys
import time
import threading
from pathlib import Path
from typing import Dict, Any, Iterable, Set, List, Tuple, Optional

import networkx as nx

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None  # fallback to plain prints

# Optional: memory/cpu monitor
try:
    import psutil
except ImportError:
    psutil = None

# Reuse your existing builder implementation
from multilevel_graph_builder import process_repo_to_graph


# ------------------------- IO helpers -------------------------

def iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for ln, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON at {path}:{ln}: {e}") from e


def collect_repo_dirs(tasks_jsonl: Path) -> List[str]:
    repo_dirs: Set[str] = set()
    for item in iter_jsonl(tasks_jsonl):
        rd = item.get("repo_dir")
        if rd:
            repo_dirs.add(str(rd))
    return sorted(repo_dirs)


def write_graphml(g: nx.Graph, out_path: Path) -> None:
    """
    Prefer lxml writer if available (handles more attrs in practice).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        nx.write_graphml_lxml(g, out_path)  # type: ignore[attr-defined]
    except Exception:
        nx.write_graphml(g, out_path)


# ------------------------- Progress plumbing -------------------------

class RepoProgress:
    """Holds current step for one repo (updated by callback)."""
    def __init__(self):
        self.step = "init"
        self.detail = ""
        self.lock = threading.Lock()

    def update(self, step: str, detail: str = ""):
        with self.lock:
            self.step = step
            self.detail = detail

    def snapshot(self):
        with self.lock:
            return self.step, self.detail


def make_progress_cb(rp: RepoProgress):
    """
    Callback used by multilevel_graph_builder (after you apply patch).
    Expected signature: progress_cb(step: str, status: str = "start"/"end", elapsed: float = 0.0, **kw)
    But we accept any kwargs safely.
    """
    def _cb(step: str, **kw):
        status = kw.get("status", "")
        elapsed = kw.get("elapsed", None)
        msg = status
        if elapsed is not None and status == "end":
            msg = f"{status} {elapsed:.2f}s"
        rp.update(step, msg)
    return _cb


def start_heartbeat(repo_dir: str, rp: RepoProgress, interval_sec: int = 5):
    """
    Prints a heartbeat so you can see it is still alive even if a step is slow.
    """
    stop_flag = {"stop": False}
    start_t = time.perf_counter()

    proc = psutil.Process(os.getpid()) if psutil else None

    def _loop():
        while not stop_flag["stop"]:
            time.sleep(interval_sec)
            step, detail = rp.snapshot()
            elapsed = time.perf_counter() - start_t

            extra = ""
            if proc:
                try:
                    rss_gb = proc.memory_info().rss / (1024 ** 3)
                    cpu = proc.cpu_percent(interval=None)  # needs prior call; still ok as rough
                    extra = f", RSS={rss_gb:.2f}GB, CPU%~{cpu:.0f}"
                except Exception:
                    pass

            print(f"[HB] {repo_dir} | step={step} {detail} | elapsed={elapsed:.1f}s{extra}", flush=True)

    th = threading.Thread(target=_loop, daemon=True)
    th.start()

    def stop():
        stop_flag["stop"] = True

    return stop


# ------------------------- Timeout runner -------------------------

def run_with_timeout(fn, timeout_sec: int):
    """
    Run fn() in current process with SIGALRM timeout (Linux only).
    """
    if timeout_sec <= 0:
        return fn()

    if not hasattr(signal, "SIGALRM"):
        # fallback: no timeout
        return fn()

    def _handler(signum, frame):
        raise TimeoutError(f"Timeout after {timeout_sec}s")

    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(timeout_sec)
    try:
        return fn()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# ------------------------- Main -------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Build GRACE repo_multi_graph.graphml per repo_dir (repo-level, reusable) with progress."
    )
    ap.add_argument("--tasks_jsonl", required=True, help="Step1 output jsonl; each line contains repo_dir")
    ap.add_argument("--repos_root", default="/w/P/CERepos", help="Root directory containing repos/<repo_dir>/")
    ap.add_argument("--graphs_root", required=True, help="Output root for graphs; writes <graphs_root>/<repo_dir>/repo_multi_graph.graphml")
    ap.add_argument("--language", default="python", choices=["python", "java"], help="Repo language for graph builder")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing graphml files")
    ap.add_argument("--max_repos", type=int, default=0, help="If >0, process only first N repos (debug)")
    ap.add_argument("--stop_on_error", action="store_true", help="Stop immediately if any repo fails")
    ap.add_argument("--heartbeat_sec", type=int, default=15, help="Heartbeat interval in seconds (0 disables)")
    ap.add_argument("--timeout_sec", type=int, default=0, help="Per-repo timeout seconds (0 disables)")
    args = ap.parse_args()

    tasks_jsonl = Path(args.tasks_jsonl)
    repos_root = Path(args.repos_root)
    graphs_root = Path(args.graphs_root)

    if not tasks_jsonl.exists():
        raise FileNotFoundError(f"tasks_jsonl not found: {tasks_jsonl}")
    if not repos_root.exists():
        raise FileNotFoundError(f"repos_root not found: {repos_root}")

    repo_dirs = collect_repo_dirs(tasks_jsonl)
    if args.max_repos and args.max_repos > 0:
        repo_dirs = repo_dirs[: args.max_repos]

    total = len(repo_dirs)
    ok = 0
    skipped = 0
    failed = 0
    failures: List[Tuple[str, str]] = []

    print(f"Found {total} unique repos in tasks jsonl.")
    print(f"repos_root  = {repos_root}")
    print(f"graphs_root = {graphs_root}")
    print(f"language    = {args.language}")
    print(f"timeout_sec = {args.timeout_sec}")
    print(f"heartbeat   = {args.heartbeat_sec}s (0=off)")
    print()

    # overall progress bar
    use_tqdm = tqdm is not None and sys.stderr.isatty()
    pbar = tqdm(total=total, desc="Repos", unit="repo") if use_tqdm else None

    for i, repo_dir in enumerate(repo_dirs, start=1):
        repo_path = repos_root / repo_dir
        out_dir = graphs_root / repo_dir
        out_graphml = out_dir / "repo_multi_graph.graphml"

        rp = RepoProgress()
        progress_cb = make_progress_cb(rp)

        # update top bar
        if pbar:
            pbar.set_postfix_str(repo_dir)
        else:
            print(f"\n[{i}/{total}] repo_dir={repo_dir}")

        if not repo_path.exists():
            msg = f"Repo path not found: {repo_path}"
            print(f"  [FAIL] {msg}")
            failed += 1
            failures.append((repo_dir, msg))
            if args.stop_on_error:
                break
            if pbar: pbar.update(1)
            continue

        if out_graphml.exists() and not args.overwrite:
            if not pbar:
                print(f"  [SKIP] graph already exists: {out_graphml}")
            skipped += 1
            if pbar: pbar.update(1)
            continue

        stop_hb = None
        if args.heartbeat_sec and args.heartbeat_sec > 0:
            stop_hb = start_heartbeat(repo_dir, rp, interval_sec=args.heartbeat_sec)

        t0 = time.perf_counter()

        try:
            def _do_build():
                # If you apply patch, builder will accept progress_cb; if not, it will ignore via try/except below
                try:
                    return process_repo_to_graph(str(repo_path), lang=args.language, progress_cb=progress_cb)
                except TypeError:
                    # Old signature: no progress_cb support
                    return process_repo_to_graph(str(repo_path), lang=args.language)

            graphs = run_with_timeout(_do_build, args.timeout_sec)

            if graphs.combined_graph is None:
                msg = "combined_graph is None after process_repo_to_graph"
                print(f"  [FAIL] {msg}")
                failed += 1
                failures.append((repo_dir, msg))
                if args.stop_on_error:
                    break
                continue

            rp.update("write_graphml", "start")
            write_graphml(graphs.combined_graph, out_graphml)
            rp.update("write_graphml", "end")

            dt = time.perf_counter() - t0
            if not pbar:
                print(f"  [OK] wrote: {out_graphml} ({dt:.1f}s)")
            ok += 1

        except TimeoutError as e:
            msg = str(e)
            print(f"  [TIMEOUT] {repo_dir}: {msg}")
            failed += 1
            failures.append((repo_dir, msg))
            if args.stop_on_error:
                break

        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            print(f"  [FAIL] {msg}")
            failed += 1
            failures.append((repo_dir, msg))
            if args.stop_on_error:
                break

        finally:
            if stop_hb:
                stop_hb()
            if pbar:
                # show current step after finishing
                step, detail = rp.snapshot()
                pbar.set_postfix_str(f"{repo_dir} | last={step} {detail}")
                pbar.update(1)

    if pbar:
        pbar.close()

    print("\n==== Summary ====")
    print(f"Total repos: {total}")
    print(f"OK:          {ok}")
    print(f"Skipped:     {skipped}")
    print(f"Failed:      {failed}")
    if failures:
        print("\nFailures (up to 50 shown):")
        for rd, msg in failures[:50]:
            print(f" - {rd}: {msg}")


if __name__ == "__main__":
    main()
