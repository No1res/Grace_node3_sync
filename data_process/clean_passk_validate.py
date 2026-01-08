#!/usr/bin/env python3
import argparse
import os
import shutil
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos_root", type=str, required=True, help="e.g. /w/P/CERepos")
    ap.add_argument("--pattern", type=str, default="*_passk_validte*.py")
    ap.add_argument("--quarantine_dir", type=str, required=True, help="e.g. /w/P/quarantine_passk_validate")
    ap.add_argument("--delete", action="store_true", help="If set, delete instead of moving to quarantine")
    args = ap.parse_args()

    repos_root = Path(args.repos_root).resolve()
    quarantine = Path(args.quarantine_dir).resolve()
    quarantine.mkdir(parents=True, exist_ok=True)

    matched = []
    # 遍历所有 repo_dir
    for repo_dir in repos_root.iterdir():
        if not repo_dir.is_dir():
            continue
        # 在 repo 内递归匹配
        for p in repo_dir.rglob(args.pattern):
            if p.is_file():
                matched.append(p)

    print(f"[INFO] repos_root={repos_root}")
    print(f"[INFO] pattern={args.pattern}")
    print(f"[INFO] matched_files={len(matched)}")

    for src in matched:
        rel = src.relative_to(repos_root)  # repo_dir/...
        if args.delete:
            try:
                src.unlink()
                print(f"[DEL] {rel}")
            except Exception as e:
                print(f"[ERR] delete failed: {rel} :: {e}")
        else:
            dst = quarantine / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), str(dst))
                print(f"[MOVE] {rel} -> {dst.relative_to(quarantine)}")
            except Exception as e:
                print(f"[ERR] move failed: {rel} :: {e}")

    print("[DONE]")

if __name__ == "__main__":
    main()

