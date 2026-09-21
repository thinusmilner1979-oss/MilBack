import json
import os
from datetime import datetime, timedelta

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "milback")
CONFIG_FILE = os.path.join(CONFIG_DIR, "backup_profiles.json")

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DEFAULTS = {
    "jobs": [],
    "mode": "Add & Update (Incremental)",
    "deep": False,
    "quick_verify": False,
    "retries": 5,
    "wait": 30,
    "workers": 4,
    "versioning": "Overwrite in place",
    "retention_days": 30,
    "trust_index": False,
    "max_delete_ratio": 0.20,
    "allow_large_deletes": False,
    "sched_type": "Manual",
    "sched_day": "Monday",
    "sched_time": "00:00",
    "catch_up": True,
}


def new_profile():
    profile = dict(DEFAULTS)
    profile["jobs"] = []
    return profile


def load():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    for name, profile in data.items():
        merged = dict(DEFAULTS)
        merged.update(profile)
        data[name] = merged
    return data


def save(profiles):
    # Written to a temp file and renamed so a crash mid-write cannot leave the
    # user with a truncated profile list.
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_FILE)


def to_settings(profile):
    return {
        "jobs": profile.get("jobs", []),
        "backup_mode": profile.get("mode", DEFAULTS["mode"]),
        "deep_verify": profile.get("deep", False),
        "quick_verify": profile.get("quick_verify", False),
        "retries": profile.get("retries", 5),
        "wait_timeout": profile.get("wait", 30) * 60,
        "workers": profile.get("workers", 4),
        "versioning": profile.get("versioning", DEFAULTS["versioning"]),
        "retention_days": profile.get("retention_days", 30),
        "trust_index": profile.get("trust_index", False),
        "max_delete_ratio": profile.get("max_delete_ratio", 0.20),
        "allow_large_deletes": profile.get("allow_large_deletes", False),
    }


def due_since(profile, now=None):
    """The most recent moment this profile was supposed to run, or None.

    Returning a time rather than a yes/no is what lets a missed slot still fire:
    comparing it to the last successful run catches up a backup that was due
    while the machine was asleep, instead of skipping it until tomorrow."""
    now = now or datetime.now()
    kind = profile.get("sched_type", "Manual")
    if kind == "Manual":
        return None
    try:
        hour, minute = (int(p) for p in profile.get("sched_time", "00:00").split(":"))
    except ValueError:
        return None

    if kind == "Daily":
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate > now:
            candidate -= timedelta(days=1)
        return candidate

    if kind == "Weekly":
        try:
            wanted = DAYS.index(profile.get("sched_day", "Monday"))
        except ValueError:
            return None
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = (candidate.weekday() - wanted) % 7
        candidate -= timedelta(days=delta)
        if candidate > now:
            candidate -= timedelta(days=7)
        return candidate

    return None


def is_due(profile, last_success_ts, now=None, grace_minutes=10):
    scheduled = due_since(profile, now)
    if scheduled is None:
        return False
    scheduled_ts = scheduled.timestamp()
    if last_success_ts and last_success_ts >= scheduled_ts:
        return False
    if not profile.get("catch_up", True):
        now = now or datetime.now()
        if (now.timestamp() - scheduled_ts) > grace_minutes * 60:
            return False
    return True
