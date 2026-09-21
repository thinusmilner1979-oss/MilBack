import json
import os
import time

from index import STATE_DIR

LOG_DIR = os.path.join(STATE_DIR, "logs")
STATUS_FILE = os.path.join(STATE_DIR, "last_runs.json")


def _safe_name(text):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)[:60]


class RunLog:
    def __init__(self, profile_name, keep_runs=50):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.profile = profile_name
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(LOG_DIR, f"{_safe_name(profile_name)}-{stamp}.log")
        self.started = time.time()
        self._fh = open(self.path, "a", buffering=1, encoding="utf-8")
        self.write(f"MilBack run: {profile_name}")
        self._prune(keep_runs)

    def write(self, line):
        try:
            self._fh.write(f"{time.strftime('%H:%M:%S')}  {line}\n")
        except (OSError, ValueError):
            pass

    def close(self, summary):
        summary = dict(summary)
        summary["profile"] = self.profile
        summary["started"] = self.started
        summary["finished"] = time.time()
        summary["duration"] = summary["finished"] - self.started
        summary["log"] = self.path
        self.write(f"SUMMARY {json.dumps(summary, default=str)}")
        try:
            self._fh.close()
        except (OSError, ValueError):
            pass
        _record_status(self.profile, summary)
        return summary

    def _prune(self, keep):
        prefix = _safe_name(self.profile) + "-"
        try:
            mine = sorted(f for f in os.listdir(LOG_DIR)
                          if f.startswith(prefix) and f.endswith(".log"))
        except OSError:
            return
        for name in mine[:-keep] if len(mine) > keep else []:
            try:
                os.remove(os.path.join(LOG_DIR, name))
            except OSError:
                pass


def _record_status(profile, summary):
    data = load_status()
    data[profile] = summary
    tmp = STATUS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, STATUS_FILE)
    except OSError:
        pass


def load_status():
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
