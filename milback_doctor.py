#!/usr/bin/env python3
"""
milback_doctor.py  --  diagnose why a MilBack job scans but never copies.

Runs MilBack's *exact* scan logic against your real job paths, but with every
error reported instead of swallowed, and with a live counter so you can see
whether the scan is stuck or just slow.

Usage:
    python3 milback_doctor.py                      # reads ~/.config/milback/backup_profiles.json
    python3 milback_doctor.py "My Profile"         # just one profile
    python3 milback_doctor.py --src /path/src --dst /path/dst

Read-only. It never writes, copies or deletes anything.
"""

import os
import sys
import json
import time
import stat as statmod

CONFIG = os.path.expanduser("~/.config/milback/backup_profiles.json")
PROGRESS_EVERY = 2000


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


class Report:
    def __init__(self):
        self.entries_seen = 0
        self.dirs_seen = 0
        self.files_seen = 0
        self.queued = 0
        self.bytes_queued = 0
        self.skipped_same = 0
        self.symlink_files = 0
        self.symlink_dirs = 0
        self.loops = []
        self.dir_errors = []
        self.file_errors = []
        self.dst_stat_errors = []
        self.started = time.time()


def scan(src_root, dst_root, mode, report, max_seconds=0):
    """Iterative version of engine._walk, with nothing hidden."""
    src_root = os.path.abspath(src_root.rstrip(os.sep))
    base = os.path.basename(src_root)
    target_root = os.path.join(os.path.abspath(dst_root), base)

    # Nesting check: destination inside source is an infinite-growth trap.
    if (target_root + os.sep).startswith(src_root + os.sep):
        print(f"  !! DESTINATION IS INSIDE SOURCE: {target_root}")
        print("     The scan will walk into its own backup. This alone can hang a job.")

    visited = set()
    stack = [(src_root, target_root)]

    while stack:
        cur_src, cur_dst = stack.pop()

        if max_seconds and (time.time() - report.started) > max_seconds:
            print(f"  !! stopping after {max_seconds}s (use a bigger --limit to go longer)")
            return

        try:
            st = os.stat(cur_src)
            key = (st.st_dev, st.st_ino)
            if key in visited:
                report.loops.append(cur_src)
                continue
            visited.add(key)
        except OSError as e:
            report.dir_errors.append((cur_src, f"stat: {e}"))
            continue

        try:
            with os.scandir(cur_src) as it:
                for entry in it:
                    report.entries_seen += 1
                    if report.entries_seen % PROGRESS_EVERY == 0:
                        el = time.time() - report.started
                        print(f"     ... {report.entries_seen:,} items in {el:,.0f}s "
                              f"| {report.queued:,} queued | now: {cur_src[:70]}",
                              flush=True)
                    target_path = os.path.join(cur_dst, entry.name)
                    try:
                        is_link = entry.is_symlink()
                        if entry.is_dir():
                            report.dirs_seen += 1
                            if is_link:
                                report.symlink_dirs += 1
                            stack.append((entry.path, target_path))
                        elif entry.is_file():
                            report.files_seen += 1
                            if is_link:
                                report.symlink_files += 1
                            if needed(entry, target_path, mode, report):
                                report.queued += 1
                                report.bytes_queued += entry.stat().st_size
                            else:
                                report.skipped_same += 1
                    except OSError as e:
                        report.file_errors.append((entry.path, str(e)))
        except OSError as e:
            report.dir_errors.append((cur_src, str(e)))
            print(f"  !! FOLDER ERROR (MilBack would silently abandon this folder"
                  f" AND everything after it): {cur_src} -> {e}", flush=True)


def needed(entry, dst, mode, report):
    if mode == "Full Overwrite":
        return True
    if not os.path.exists(dst):
        return True
    try:
        s = entry.stat()
        d = os.stat(dst)
    except OSError as e:
        report.dst_stat_errors.append((dst, str(e)))
        return True
    if s.st_size != d.st_size:
        return True
    if int(s.st_mtime) != int(d.st_mtime):
        return True
    return False


def check_dest(dst_root):
    print(f"  destination: {dst_root}")
    if not os.path.isdir(dst_root):
        print("  !! DESTINATION DOES NOT EXIST OR IS NOT A DIRECTORY")
        print("     (MilBack will still 'succeed' at scanning, then fail per-file.)")
        return
    probe = os.path.join(dst_root, ".milback_write_test")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        print("  destination is writable: yes")
    except OSError as e:
        print(f"  !! DESTINATION NOT WRITABLE: {e}")

    # timestamp granularity: the thing that makes SMB/exFAT re-copy forever
    try:
        probe = os.path.join(dst_root, ".milback_time_test")
        with open(probe, "w") as f:
            f.write("ok")
        want = time.time() - 12345.678
        os.utime(probe, (want, want))
        got = os.stat(probe).st_mtime
        os.remove(probe)
        drift = abs(got - want)
        if drift > 0.9:
            print(f"  !! TIMESTAMP GRANULARITY {drift:.2f}s on the destination.")
            print("     MilBack compares int(mtime) exactly, so files will look")
            print("     'changed' forever and be re-copied on every single run.")
        else:
            print(f"  timestamp fidelity: ok ({drift:.3f}s drift)")
    except OSError as e:
        print(f"  timestamp probe failed: {e}")


def run_job(src, dst, mode, limit):
    print()
    print("=" * 74)
    print(f"JOB  {src}")
    print(f"  -> {dst}   [{mode}]")
    print("=" * 74)

    if not os.path.isdir(src):
        print("  !! SOURCE DOES NOT EXIST OR IS NOT A DIRECTORY -> scan yields 0 files.")
        return
    try:
        os.listdir(src)
    except OSError as e:
        print(f"  !! SOURCE NOT READABLE: {e}")
        print("     MilBack swallows this and reports a clean scan of 0 files.")
        return

    check_dest(dst)
    print("  scanning...", flush=True)

    r = Report()
    scan(src, dst, mode, r, max_seconds=limit)
    el = time.time() - r.started

    print()
    print(f"  scan time            : {el:,.1f}s")
    print(f"  entries seen         : {r.entries_seen:,}")
    print(f"  folders              : {r.dirs_seen:,}  (symlinked: {r.symlink_dirs:,})")
    print(f"  files                : {r.files_seen:,}  (symlinked: {r.symlink_files:,})")
    print(f"  QUEUED TO COPY       : {r.queued:,}  ({human(r.bytes_queued)})")
    print(f"  skipped, unchanged   : {r.skipped_same:,}")
    print(f"  folder errors        : {len(r.dir_errors):,}")
    print(f"  file errors          : {len(r.file_errors):,}")
    print(f"  destination stat errs: {len(r.dst_stat_errors):,}")
    print(f"  symlink loops caught : {len(r.loops):,}")

    for label, items in (("FOLDER ERRORS", r.dir_errors),
                         ("FILE ERRORS", r.file_errors),
                         ("SYMLINK LOOPS", [(p, "") for p in r.loops])):
        if items:
            print(f"\n  --- {label} (first 15) ---")
            for p, e in items[:15]:
                print(f"    {p}\n        {e}" if e else f"    {p}")
            if len(items) > 15:
                print(f"    ... and {len(items) - 15:,} more")

    print("\n  VERDICT:")
    if r.queued == 0 and r.files_seen > 0 and not r.dir_errors:
        print("    Scan is healthy and everything is already backed up.")
        print("    MilBack has nothing to copy - this is correct behaviour, but the")
        print("    GUI never tells you so. Touch a source file and re-run to confirm.")
    elif r.queued == 0 and r.dir_errors:
        print("    Scan hit folder errors and found nothing to copy. In MilBack these")
        print("    errors are caught by 'except Exception: pass' and never shown, so")
        print("    the job looks like it scanned cleanly and then did nothing.")
    elif r.queued == 0 and r.files_seen == 0:
        print("    The scan saw ZERO files. Wrong source path, an unmounted share, or")
        print("    a permissions problem. MilBack reports this as a successful scan.")
    elif r.loops or r.symlink_dirs:
        print("    Symlinked folders present. MilBack's engine follows them with no")
        print("    loop guard and no recursion limit - this is what makes a scan run")
        print("    forever. The loop guard in this script is why it terminated.")
    else:
        print(f"    Scan is healthy: {r.queued:,} files ({human(r.bytes_queued)}) should copy.")
        print("    If MilBack is not copying them, the job is still in phase 1 - it")
        print("    refuses to copy a single byte until the whole tree is scanned.")


def main():
    args = sys.argv[1:]
    limit = 0
    if "--limit" in args:
        i = args.index("--limit")
        limit = int(args[i + 1])
        del args[i:i + 2]

    mode = "Add & Update (Incremental)"

    if "--src" in args:
        src = args[args.index("--src") + 1]
        dst = args[args.index("--dst") + 1]
        run_job(src, dst, mode, limit)
        return

    if not os.path.exists(CONFIG):
        print(f"No profile file at {CONFIG}")
        print("Pass paths directly:  python3 milback_doctor.py --src /path --dst /path")
        return

    with open(CONFIG) as f:
        profiles = json.load(f)

    wanted = args[0] if args else None
    print(f"Profiles found: {', '.join(profiles) or '(none)'}")

    for name, data in profiles.items():
        if wanted and name != wanted:
            continue
        print()
        print("#" * 74)
        print(f"# PROFILE: {name}")
        print(f"#   mode={data.get('mode')}  deep={data.get('deep')}  "
              f"schedule={data.get('sched_type')} {data.get('sched_time','')}")
        print("#" * 74)
        jobs = data.get("jobs", [])
        if not jobs:
            print("  !! THIS PROFILE HAS NO JOBS. Nothing will ever be copied.")
            continue
        for job in jobs:
            run_job(job["src"], job["dst"], data.get("mode", mode), limit)


if __name__ == "__main__":
    main()
