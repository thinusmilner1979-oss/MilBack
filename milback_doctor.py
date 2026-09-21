#!/usr/bin/env python3
"""Check a MilBack job's environment before blaming the backup.

Measures what actually makes a backup over a network share slow: metadata
round-trip latency, directory listing cost and raw transfer rate. Reports a
suggested number of parallel transfers based on those numbers.

    python3 milback_doctor.py                       # every configured profile
    python3 milback_doctor.py "My Profile"
    python3 milback_doctor.py --src /path --dst /path

Read-only apart from a few small probe files it deletes again.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import profiles as profile_store
from version import version_string

PROBE_COUNT = 30


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def measure_stat_latency(directory, names):
    if not names:
        return None
    start = time.perf_counter()
    done = 0
    for name in names[:PROBE_COUNT]:
        try:
            os.stat(os.path.join(directory, name))
            done += 1
        except OSError:
            pass
    if not done:
        return None
    return (time.perf_counter() - start) / done


def measure_write(directory):
    probe = os.path.join(directory, ".milback_probe")
    payload = os.urandom(4 * 1024 * 1024)
    try:
        start = time.perf_counter()
        with open(probe, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        elapsed = time.perf_counter() - start
        return len(payload) / elapsed if elapsed else None
    except OSError:
        return None
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def measure_timestamp_fidelity(directory):
    probe = os.path.join(directory, ".milback_time_probe")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        wanted = time.time() - 12345.678
        os.utime(probe, (wanted, wanted))
        got = os.stat(probe).st_mtime
        return abs(got - wanted)
    except OSError:
        return None
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def walk_sample(root, seconds=5.0):
    files = dirs = 0
    total = 0
    deepest_names = []
    started = time.perf_counter()
    stack = [root]
    while stack and (time.perf_counter() - started) < seconds:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                names = []
                for entry in it:
                    try:
                        if entry.is_dir():
                            dirs += 1
                            stack.append(entry.path)
                        elif entry.is_file():
                            files += 1
                            total += entry.stat().st_size
                            names.append(entry.name)
                    except OSError:
                        pass
                if len(names) > len(deepest_names):
                    deepest_names = names
                    deepest_dir = current
        except OSError:
            pass
    elapsed = time.perf_counter() - started
    return {
        "files": files, "dirs": dirs, "bytes": total, "elapsed": elapsed,
        "complete": not stack,
        "sample_dir": locals().get("deepest_dir", root),
        "sample_names": deepest_names,
    }


def check_job(src, dst, mode):
    print()
    print("=" * 72)
    print(f"{src}")
    print(f"  -> {dst}   [{mode}]")
    print("=" * 72)

    for label, path in (("source", src), ("destination", dst)):
        if not os.path.isdir(path):
            print(f"  !! {label} is missing or not a directory: {path}")
            return
    if os.path.abspath(dst).startswith(os.path.abspath(src) + os.sep):
        print("  !! the destination is inside the source; MilBack will skip it, "
              "but pick a separate folder")

    write_speed = measure_write(dst)
    if write_speed is None:
        print("  !! destination is not writable")
        return
    print(f"  write speed          : {human(write_speed)}/s")

    drift = measure_timestamp_fidelity(dst)
    if drift is None:
        print("  timestamp fidelity   : could not measure")
    elif drift > 0.9:
        print(f"  timestamp fidelity   : {drift:.2f}s granularity "
              f"(within MilBack's 2s tolerance)")
    else:
        print(f"  timestamp fidelity   : exact ({drift:.3f}s)")

    print("  walking the source for up to 5s...", flush=True)
    scan = walk_sample(src)
    rate = scan["files"] / scan["elapsed"] if scan["elapsed"] else 0
    print(f"  scan rate            : {rate:,.0f} files/s "
          f"({scan['files']:,} files, {scan['dirs']:,} folders seen)")
    if not scan["complete"]:
        print("                         (sample only; the tree is larger than 5s)")

    latency = measure_stat_latency(scan["sample_dir"], scan["sample_names"])
    if latency:
        print(f"  metadata round trip  : {latency * 1000:.2f} ms per file")
        if latency > 0.002:
            suggested = min(16, max(4, int(latency / 0.002) * 2))
            print(f"  -> latency-bound. Try {suggested} parallel transfers and turn on "
                  f"fast incremental.")
        else:
            print("  -> metadata is fast; 2-4 parallel transfers is plenty.")

    if scan["files"] and scan["complete"]:
        print(f"  source size          : {human(scan['bytes'])} in {scan['files']:,} files")
        if write_speed:
            print(f"  full copy would take : about "
                  f"{scan['bytes'] / write_speed / 60:.0f} min at the measured speed")


def main():
    print(f"{version_string()} doctor")
    args = [a for a in sys.argv[1:]]
    if "--src" in args:
        check_job(args[args.index("--src") + 1], args[args.index("--dst") + 1],
                  "Add & Update (Incremental)")
        return 0

    all_profiles = profile_store.load()
    if not all_profiles:
        print(f"No profiles in {profile_store.CONFIG_FILE}")
        print("Pass paths directly: milback_doctor.py --src /path --dst /path")
        return 1

    wanted = args[0] if args else None
    for name, profile in sorted(all_profiles.items()):
        if wanted and name != wanted:
            continue
        print()
        print("#" * 72)
        print(f"# {name}   ({profile.get('sched_type', 'Manual')}, "
              f"{len(profile.get('jobs', []))} job(s))")
        print("#" * 72)
        if not profile.get("jobs"):
            print("  !! no jobs configured; nothing will ever be copied")
        for job in profile.get("jobs", []):
            check_job(job["src"], job["dst"], profile.get("mode"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
