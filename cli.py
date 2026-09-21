import argparse
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEventLoop

import profiles as profile_store
from engine import BackupWorker
from runlog import load_status


def run_profile(name, profile, dry_run=False, quiet=False):
    settings = profile_store.to_settings(profile)
    settings["dry_run"] = dry_run
    worker = BackupWorker(settings, name)
    if not quiet:
        worker.progress_update.connect(lambda line: print(line, flush=True))
    worker.error_found.connect(lambda line: print(line, file=sys.stderr, flush=True))

    loop = QEventLoop()
    worker.finished.connect(lambda *_: loop.quit())
    worker.start()
    loop.exec()
    worker.wait(30000)

    status = load_status().get(name, {})
    failed = status.get("files_failed", 0) + status.get("scan_errors", 0)
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="milback-cli",
        description="Run MilBack profiles without the GUI.")
    parser.add_argument("profile", nargs="?",
                        help="profile name; omit to run every scheduled profile "
                             "that is currently due")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would happen without writing anything")
    parser.add_argument("--all", action="store_true",
                        help="run every profile regardless of schedule")
    parser.add_argument("--list", action="store_true",
                        help="list profiles and their last run")
    parser.add_argument("--quiet", action="store_true",
                        help="only print errors")
    args = parser.parse_args(argv)

    # Kept in a local: an unreferenced QCoreApplication is collected, and the
    # event loop the worker needs then refuses to start.
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    assert app is not None

    all_profiles = profile_store.load()
    status = load_status()

    if args.list:
        if not all_profiles:
            print(f"No profiles in {profile_store.CONFIG_FILE}")
            return 0
        for name, profile in sorted(all_profiles.items()):
            record = status.get(name, {})
            when = record.get("finished")
            stamp = ("never" if not when else
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(when)))
            print(f"{name}\t{profile.get('sched_type', 'Manual')}\tlast run: {stamp}")
        return 0

    if args.profile:
        if args.profile not in all_profiles:
            print(f"No such profile: {args.profile}", file=sys.stderr)
            return 2
        selected = [(args.profile, all_profiles[args.profile])]
    elif args.all:
        selected = sorted(all_profiles.items())
    else:
        selected = []
        for name, profile in sorted(all_profiles.items()):
            record = status.get(name) or {}
            last = 0 if record.get("dry_run") else record.get("finished", 0)
            if profile_store.is_due(profile, last):
                selected.append((name, profile))
        if not selected and not args.quiet:
            print("Nothing is due.")

    worst = 0
    for name, profile in selected:
        if not args.quiet:
            print(f"=== {name} ===", flush=True)
        worst = max(worst, run_profile(name, profile, args.dry_run, args.quiet))
    return worst


if __name__ == "__main__":
    sys.exit(main())
