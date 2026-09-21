import errno
import hashlib
import os
import queue
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import QThread, pyqtSignal

from index import StateIndex, job_key
from runlog import RunLog

# Retrying these just burns the retry budget: the read will never succeed.
NON_RETRYABLE = (errno.EACCES, errno.EPERM, errno.EIO, errno.ENOENT, errno.EISDIR)

PART_SUFFIX = ".milback-part"
VERSIONS_DIR = ".milback-versions"
SNAPSHOT_DIR = ".milback-snapshots"

MODE_INCREMENTAL = "Add & Update (Incremental)"
MODE_MIRROR = "Exact Sync (Mirror)"
MODE_OVERWRITE = "Full Overwrite"

VERSION_NONE = "Overwrite in place"
VERSION_KEEP = "Keep previous versions"
VERSION_SNAPSHOT = "Hardlink snapshots"


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def file_digest(path, full=True, chunk=1024 * 1024):
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as f:
            if full:
                for block in iter(lambda: f.read(chunk), b""):
                    h.update(block)
            else:
                h.update(f.read(chunk))
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size > chunk:
                    f.seek(size - chunk)
                    h.update(f.read(chunk))
        return h.hexdigest()
    except OSError:
        return None


class _Counters:
    def __init__(self):
        self.lock = threading.Lock()
        self.bytes_done = 0
        self.files_done = 0
        self.files_found = 0
        self.bytes_found = 0
        self.failed = 0
        self.in_use = 0
        self.skipped = 0
        self.entries_seen = 0


class BackupWorker(QThread):
    progress_update = pyqtSignal(str)
    error_found = pyqtSignal(str)
    task_stats_ready = pyqtSignal(int, object)
    chunk_finished = pyqtSignal(object)
    # Shadows QThread.finished on purpose; main.py connects to this one.
    finished = pyqtSignal(int, int, int)

    SCAN_REPORT_EVERY = 2000
    MAX_ERRORS_SHOWN = 50
    UI_HZ = 10

    def __init__(self, settings, profile_name="backup"):
        super().__init__()
        s = settings
        self.profile_name = profile_name
        self.jobs = s.get("jobs", [])
        self.backup_mode = s.get("backup_mode", s.get("mode", MODE_INCREMENTAL))
        self.deep_verify = s.get("deep_verify", s.get("deep", False))
        self.quick_verify = s.get("quick_verify", False)
        self.retries = s.get("retries", 5)
        self.follow_links = s.get("follow_links", True)
        self.mtime_tolerance = s.get("mtime_tolerance", 2)
        self.workers = max(1, int(s.get("workers", 4)))
        self.dry_run = s.get("dry_run", False)
        self.versioning = s.get("versioning", VERSION_NONE)
        self.retention_days = int(s.get("retention_days", 30))
        self.trust_index = s.get("trust_index", False)
        self.verify_interval_days = int(s.get("verify_interval_days", 7))
        self.max_delete_ratio = float(s.get("max_delete_ratio", 0.20))
        self.allow_large_deletes = s.get("allow_large_deletes", False)
        self.buffer_size = int(s.get("buffer_size", 1024 * 1024))

        self.is_running = True
        self.c = _Counters()
        self.scan_errors = []
        self.copy_errors = []
        self._errors_emitted = 0
        self._last_ui = 0.0
        self._emit_lock = threading.Lock()
        self._index = None
        self._log = None
        self._queue = queue.Queue(maxsize=2000)
        self._index_writes = []
        self._deleted = 0
        self._job_errors = 0

    def stop(self):
        self.is_running = False

    def _say(self, message):
        if self._log:
            self._log.write(message)
        self.progress_update.emit(message)

    def _chatter(self, message):
        # Per-file noise only. Anything a user might need to act on goes through
        # _say, because throttling can drop a message that is only sent once.
        if self._log:
            self._log.write(message)
        now = time.time()
        if (now - self._last_ui) > (1.0 / self.UI_HZ):
            self._last_ui = now
            self.progress_update.emit(message)

    def _fail(self, message, bucket):
        bucket.append(message)
        if self._log:
            self._log.write("ERROR " + message)
        with self._emit_lock:
            if self._errors_emitted < self.MAX_ERRORS_SHOWN:
                self._errors_emitted += 1
                self.error_found.emit(message)
            elif self._errors_emitted == self.MAX_ERRORS_SHOWN:
                self._errors_emitted += 1
                self.error_found.emit(
                    "... further errors suppressed; the run log has them all.")

    def run(self):
        self._log = RunLog(self.profile_name)
        self._index = StateIndex()
        started = time.time()
        label = "DRY RUN" if self.dry_run else "BACKUP"
        self._say(f"--- {label}: {self.profile_name} ---")

        if not self.jobs:
            self._fail("This profile has no jobs. Nothing to back up.", self.scan_errors)
            self._finish(started)
            return

        pool = ThreadPoolExecutor(max_workers=self.workers)
        futures = [pool.submit(self._copy_worker) for _ in range(self.workers)]
        try:
            for job in self.jobs:
                if not self.is_running:
                    break
                self._run_job(job)
        finally:
            for _ in futures:
                self._queue.put(None)
            pool.shutdown(wait=True)

        if self._index_writes:
            self._index.record_many(self._index_writes)
            self._index.commit()
            self._index_writes = []

        self._finish(started)

    def _finish(self, started):
        c = self.c
        elapsed = max(time.time() - started, 0.001)
        summary = {
            "mode": self.backup_mode,
            "dry_run": self.dry_run,
            "files_found": c.files_found,
            "files_copied": c.files_done,
            "files_skipped": c.skipped,
            "files_failed": c.failed,
            "files_in_use": c.in_use,
            "files_deleted": self._deleted,
            "bytes_copied": c.bytes_done,
            "scan_errors": len(self.scan_errors),
            "copy_errors": len(self.copy_errors),
            "stopped_early": not self.is_running,
        }
        self._say(
            f"--- {'DRY RUN' if self.dry_run else 'DONE'}: {c.files_done:,} copied "
            f"({human(c.bytes_done)} at {human(c.bytes_done / elapsed)}/s), "
            f"{c.skipped:,} unchanged, {c.failed:,} failed, "
            f"{self._deleted:,} removed, {len(self.scan_errors):,} scan errors ---")
        if self._log:
            self._log.close(summary)
        if self._index:
            self._index.close()
        self.finished.emit(c.files_found, c.files_done, c.in_use)

    def _run_job(self, job):
        src = os.path.abspath(job["src"].rstrip(os.sep))
        dst_base = os.path.abspath(job["dst"])
        target_root = os.path.join(dst_base, os.path.basename(src))
        key = job_key(src, dst_base)

        self._job_errors = len(self.scan_errors)

        if not os.path.isdir(src):
            self._fail(f"SOURCE MISSING: {src}", self.scan_errors)
            return
        if not os.path.isdir(dst_base):
            self._fail(f"DESTINATION MISSING: {dst_base}", self.scan_errors)
            return

        known = self._index.load_job(key) if self.trust_index else {}
        due = (time.time() - self._index.last_verify(key)) > (
            self.verify_interval_days * 86400)
        if known and due:
            self._say("Index is due for verification; checking the destination.")
            known = {}

        self._say(f"Scanning: {src}")
        seen = set()
        structure = set() if self.backup_mode == MODE_MIRROR else None
        self._walk(src, target_root, dst_base, key, known, seen, structure)

        self._queue.join()

        if not self.is_running:
            return

        if self.trust_index:
            self._index.prune_missing(key, seen)
        if known or self.trust_index:
            self._index.mark_verified(key)

        if self.backup_mode == MODE_MIRROR:
            self._mirror_cleanup(src, target_root, structure, len(seen))

        if self.versioning != VERSION_NONE and not self.dry_run:
            self.prune_versions(target_root)

    def _listdir(self, path):
        delay = 1
        for attempt in range(self.retries + 1):
            if not self.is_running:
                return None
            try:
                with os.scandir(path) as it:
                    return list(it)
            except OSError as e:
                if e.errno in (errno.EACCES, errno.EPERM, errno.ENOENT, errno.ENOTDIR):
                    self._fail(f"FOLDER SKIPPED: {path} - {e.strerror}", self.scan_errors)
                    return None
                if attempt < self.retries:
                    self._say(f"Folder unreadable ({e.strerror}), retry "
                              f"{attempt + 1}/{self.retries}: {path}")
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                else:
                    self._fail(f"FOLDER FAILED after {self.retries} retries: "
                               f"{path} - {e.strerror}", self.scan_errors)
                    return None
        return None

    @staticmethod
    def _dest_listing(path):
        # One directory listing instead of a stat per file. On a share that is
        # one round trip for the whole folder rather than one for each entry.
        try:
            with os.scandir(path) as it:
                out = {}
                for e in it:
                    try:
                        st = e.stat()
                        out[e.name] = (st.st_size, st.st_mtime)
                    except OSError:
                        pass
                return out
        except OSError:
            return {}

    def _walk(self, src_root, dst_root, dst_base, key, known, seen, structure):
        visited = set()
        stack = [(src_root, dst_root)]
        forbidden = [p.rstrip(os.sep) for p in (dst_base, dst_root) if p]

        while stack and self.is_running:
            current_src, current_dst = stack.pop()

            here = os.path.abspath(current_src).rstrip(os.sep)
            if any(here == f or here.startswith(f + os.sep) for f in forbidden):
                self._say(f"Not descending into the destination: {current_src}")
                continue

            try:
                st = os.stat(current_src)
                ident = (st.st_dev, st.st_ino)
                if ident in visited:
                    self._say(f"Symlink loop skipped: {current_src}")
                    continue
                visited.add(ident)
            except OSError as e:
                self._fail(f"FOLDER SKIPPED: {current_src} - {e.strerror}", self.scan_errors)
                continue

            entries = self._listdir(current_src)
            if entries is None:
                continue

            dest_files = None
            for entry in entries:
                if not self.is_running:
                    return
                self.c.entries_seen += 1
                if self.c.entries_seen % self.SCAN_REPORT_EVERY == 0:
                    self._chatter(f"Scanning... {self.c.entries_seen:,} examined, "
                                  f"{self.c.files_found:,} queued")

                target_path = os.path.join(current_dst, entry.name)
                try:
                    if entry.is_symlink() and not self.follow_links:
                        continue
                    if structure is not None:
                        structure.add(target_path)
                    if entry.is_dir():
                        stack.append((entry.path, target_path))
                        continue
                    if not entry.is_file():
                        continue

                    st = entry.stat()
                    seen.add(entry.path)

                    if self._unchanged(entry.path, st, known):
                        self.c.skipped += 1
                        continue

                    if dest_files is None:
                        dest_files = self._dest_listing(current_dst)

                    if self._needs_copy(st, dest_files.get(entry.name),
                                        entry.path, target_path):
                        with self.c.lock:
                            self.c.files_found += 1
                            self.c.bytes_found += st.st_size
                        self._queue.put({
                            "src": entry.path, "dst": target_path,
                            "size": st.st_size, "mtime": st.st_mtime, "job": key,
                        })
                    else:
                        self.c.skipped += 1
                        self._index_writes.append(
                            (key, entry.path, target_path, st.st_size, st.st_mtime, None))
                except OSError as e:
                    self._fail(f"SKIPPED: {entry.path} - {e.strerror}", self.scan_errors)

    def _unchanged(self, src, st, known):
        record = known.get(src)
        if not record:
            return False
        size, mtime, _ = record
        return size == st.st_size and abs(mtime - st.st_mtime) <= self.mtime_tolerance

    def _needs_copy(self, st, dest_entry, src_path, dst_path):
        if self.backup_mode == MODE_OVERWRITE:
            return True
        if dest_entry is None:
            return True
        size, mtime = dest_entry
        if size != st.st_size:
            return True
        if abs(mtime - st.st_mtime) > self.mtime_tolerance:
            return True
        if self.deep_verify:
            full = not self.quick_verify
            return file_digest(src_path, full) != file_digest(dst_path, full)
        return False

    def _copy_worker(self):
        while True:
            task = self._queue.get()
            if task is None:
                self._queue.task_done()
                return
            try:
                if not self.is_running:
                    continue
                if self.dry_run:
                    self._chatter(f"would copy: {task['src']}")
                    with self.c.lock:
                        self.c.files_done += 1
                        self.c.bytes_done += task["size"]
                    continue
                if self._copy(task["src"], task["dst"], task["size"]):
                    with self.c.lock:
                        self.c.files_done += 1
                    self._index_writes.append(
                        (task["job"], task["src"], task["dst"],
                         task["size"], task["mtime"], None))
                else:
                    with self.c.lock:
                        self.c.failed += 1
            finally:
                self._queue.task_done()

    def _copy(self, src, dst, expected):
        part = dst + PART_SUFFIX
        written = 0
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
        except OSError as e:
            self._fail(f"Cannot create folder for {dst} - {e.strerror}", self.copy_errors)
            return False

        resume_from = 0
        try:
            # A partial transfer from an interrupted run is resumed rather than
            # restarted, which matters when a single file takes minutes. Only if
            # the source has not changed since, or the two halves would not match.
            if os.path.exists(part):
                got = os.path.getsize(part)
                if 0 < got < expected and os.path.getmtime(src) <= os.path.getmtime(part):
                    resume_from = got
                else:
                    self._cleanup_part(part)
        except OSError:
            resume_from = 0

        try:
            self._make_version(dst)
            mode = "r+b" if resume_from else "wb"
            with open(src, "rb") as f_in, open(part, mode) as f_out:
                if resume_from:
                    f_in.seek(resume_from)
                    f_out.seek(resume_from)
                    written = resume_from
                while self.is_running:
                    chunk = None
                    delay = 1
                    for attempt in range(self.retries + 1):
                        if not self.is_running:
                            break
                        try:
                            chunk = f_in.read(self.buffer_size)
                            break
                        except OSError as e:
                            if e.errno in (errno.EBUSY, errno.ETXTBSY):
                                with self.c.lock:
                                    self.c.in_use += 1
                                self._fail(f"In use, skipped: {src}", self.copy_errors)
                                return False
                            if e.errno in NON_RETRYABLE:
                                self._fail(f"Unreadable ({e.strerror}), skipped: {src}",
                                           self.copy_errors)
                                return False
                            if attempt < self.retries:
                                time.sleep(delay)
                                delay = min(delay * 2, 30)
                            else:
                                self._fail(f"Unreadable, skipped: {src}", self.copy_errors)
                                return False
                    if not chunk:
                        break
                    f_out.write(chunk)
                    written += len(chunk)
                    with self.c.lock:
                        self.c.bytes_done += len(chunk)
                    self._tick(len(chunk))
                f_out.flush()
                os.fsync(f_out.fileno())

            if not self.is_running:
                return False
            if written != expected:
                self._fail(f"Short copy ({written:,} of {expected:,}): {src}",
                           self.copy_errors)
                return False

            st = os.stat(src)
            os.utime(part, (st.st_atime, st.st_mtime))
            try:
                os.chmod(part, st.st_mode & 0o7777)
            except OSError:
                pass
            os.replace(part, dst)
            self._chatter(f"copied: {os.path.basename(src)}")
            return True
        except Exception as e:
            self._cleanup_part(part)
            self._fail(f"Error copying {os.path.basename(src)}: {e}", self.copy_errors)
            return False

    def _tick(self, n):
        now = time.time()
        if (now - self._last_ui) > (1.0 / self.UI_HZ):
            self._last_ui = now
            self.chunk_finished.emit(self.c.bytes_done)
            self.task_stats_ready.emit(self.c.files_found, self.c.bytes_found)

    @staticmethod
    def _cleanup_part(part):
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass

    def _version_root(self, dst):
        parts = os.path.abspath(dst).split(os.sep)
        for marker in (VERSIONS_DIR, SNAPSHOT_DIR):
            if marker in parts:
                return None
        return os.path.join(os.path.dirname(dst), VERSIONS_DIR,
                            time.strftime("%Y-%m-%d"))

    def _make_version(self, dst):
        if self.versioning == VERSION_NONE or self.dry_run:
            return
        if not os.path.exists(dst):
            return
        root = self._version_root(dst)
        if root is None:
            return
        try:
            os.makedirs(root, exist_ok=True)
            keep = os.path.join(root, os.path.basename(dst))
            if not os.path.exists(keep):
                if self.versioning == VERSION_SNAPSHOT:
                    try:
                        os.link(dst, keep)
                        return
                    except OSError:
                        pass
                shutil.copy2(dst, keep)
        except OSError as e:
            self._fail(f"Could not keep a previous version of {dst}: {e}",
                       self.copy_errors)

    def prune_versions(self, dst_base):
        if self.retention_days <= 0:
            return
        cutoff = time.time() - self.retention_days * 86400
        for root, dirs, _ in os.walk(dst_base):
            if os.path.basename(root) != VERSIONS_DIR:
                continue
            for day in dirs:
                path = os.path.join(root, day)
                try:
                    if os.path.getmtime(path) < cutoff:
                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    pass

    def _mirror_cleanup(self, src, target_root, structure, source_count):
        if len(self.scan_errors) > self._job_errors:
            self._say("--- CLEANUP SKIPPED: the scan reported errors, so the source "
                      "listing is incomplete. ---")
            return
        if self.c.failed:
            self._say("--- CLEANUP SKIPPED: some files failed to copy. ---")
            return
        # An unmounted share reads as an empty directory, not as an error. Without
        # this the mirror would treat the whole backup as surplus and delete it.
        if source_count == 0:
            self._fail(
                f"CLEANUP REFUSED: {src} scanned as empty. If the share was not "
                f"mounted, deleting the destination would destroy the backup.",
                self.scan_errors)
            return
        if not os.path.isdir(target_root):
            return

        doomed = []
        doomed_files = 0
        existing = 0
        for root, dirs, files in os.walk(target_root, topdown=False):
            if VERSIONS_DIR in root.split(os.sep) or SNAPSHOT_DIR in root.split(os.sep):
                continue
            for name in files + dirs:
                if name in (VERSIONS_DIR, SNAPSHOT_DIR) or name.endswith(PART_SUFFIX):
                    continue
                path = os.path.join(root, name)
                is_file = os.path.isfile(path)
                if is_file:
                    existing += 1
                if path not in structure:
                    doomed.append(path)
                    if is_file:
                        doomed_files += 1

        ratio = (doomed_files / existing) if existing else 0
        if doomed_files and ratio > self.max_delete_ratio and not self.allow_large_deletes:
            self._fail(
                f"CLEANUP REFUSED: {doomed_files:,} of {existing:,} destination files "
                f"({ratio:.0%}) are not in the source, above the {self.max_delete_ratio:.0%} "
                f"limit. Nothing was deleted. Enable large deletions to override.",
                self.scan_errors)
            return

        if self.dry_run:
            self._say(f"--- DRY RUN: would remove {len(doomed):,} items ---")
            self._deleted = len(doomed)
            return

        for path in doomed:
            if not self.is_running:
                break
            try:
                if self.versioning != VERSION_NONE and os.path.isfile(path):
                    self._make_version(path)
                if os.path.isfile(path):
                    os.remove(path)
                    self._deleted += 1
                elif os.path.isdir(path):
                    os.rmdir(path)
                    self._deleted += 1
            except OSError:
                pass
        self._say(f"--- CLEANUP: removed {self._deleted:,} items ---")
