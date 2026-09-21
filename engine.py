import os
import time
import errno
from PyQt6.QtCore import QThread, pyqtSignal


class BackupWorker(QThread):
    """
    MilBack backup engine.

    Drop-in replacement for the original engine.py - the signal names and
    signatures are unchanged, so main.py needs no edits.

    What changed, and why:

      1. The scan no longer hides failures. The original wrapped a whole
         directory's scandir loop in "except Exception: pass", so a single
         bad entry silently abandoned that folder AND every remaining entry
         in it. If it happened near the top of the tree the task list came
         back empty and the job "finished" having copied nothing, with no
         error anywhere. Errors are now isolated per entry and reported.

      2. The scan retries. All the network resilience used to live in the
         copy phase; a share that blinked during the scan killed the job
         silently. scandir failures now retry with backoff, same as reads.

      3. The walk is iterative, with a loop guard. The original recursed and
         followed symlinked directories with no cycle detection - a link
         pointing back up the tree (or a destination nested inside a source)
         made the scan run until it blew the recursion limit, which was then
         swallowed by the same bare except. Visited (device, inode) pairs are
         now tracked, and a destination inside its own source is skipped.

      4. The scan reports progress. It used to print one line and go quiet
         for however long a large tree over SMB takes, which is
         indistinguishable from a hang.

      5. Timestamps are compared with a tolerance. SMB, exFAT and FAT32 store
         mtime in 2-second granularity, so exact int() comparison made every
         file look changed on every run.

      6. Mirror cleanup refuses to run after a failed scan. Deleting
         destination files based on a source listing that is known to be
         incomplete is how a backup tool eats your backup.

      7. Copies are atomic. Files are written to a .milback-part temp file and
         renamed into place only after the byte count matches the source, so
         an interrupted copy can never leave a truncated file wearing the
         source's timestamp (which the incremental check would then treat as
         good forever).
    """

    progress_update = pyqtSignal(str)
    error_found = pyqtSignal(str)

    # 'object' rather than 'int' - int overflows past 2.14 GB
    task_stats_ready = pyqtSignal(int, object)
    chunk_finished = pyqtSignal(object)

    # NOTE: this shadows QThread's own finished() signal. It works, but it
    # means QThread.finished is unreachable. If you ever need it, rename this
    # to job_finished and update the connect() in main.py.
    finished = pyqtSignal(int, int, int)

    PART_SUFFIX = ".milback-part"
    SCAN_REPORT_EVERY = 2000
    MAX_ERRORS_SHOWN = 50

    def __init__(self, settings):
        super().__init__()
        self.jobs = settings.get('jobs', [])
        self.deep_verify = settings.get('deep_verify', False)
        self.retries = settings.get('retries', 5)
        self.wait_timeout = settings.get('wait_timeout', 1800)
        self.backup_mode = settings.get('backup_mode', 'Add & Update (Incremental)')
        self.follow_links = settings.get('follow_links', True)
        # SMB / exFAT / FAT32 keep mtime to 2-second resolution
        self.mtime_tolerance = settings.get('mtime_tolerance', 2)

        self.buffer_size = 1024 * 1024
        self.task_list = []
        self.total_bytes = 0
        self.source_structure = set()
        self.in_use_count = 0
        self.is_running = True

        self.scan_errors = []
        self.copy_errors = []
        self.entries_seen = 0
        self._errors_emitted = 0

    def stop(self):
        self.is_running = False

    # ------------------------------------------------------------------ util

    def _report_error(self, message, bucket):
        bucket.append(message)
        if self._errors_emitted < self.MAX_ERRORS_SHOWN:
            self._errors_emitted += 1
            self.error_found.emit(message)
        elif self._errors_emitted == self.MAX_ERRORS_SHOWN:
            self._errors_emitted += 1
            self.error_found.emit("... further errors suppressed; see the summary at the end.")

    @staticmethod
    def _human(n):
        n = float(n)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"

    # ------------------------------------------------------------------- run

    def run(self):
        self.in_use_count = 0
        self.total_bytes = 0
        self.task_list = []
        self.source_structure.clear()
        self.scan_errors = []
        self.copy_errors = []
        self.entries_seen = 0
        self._errors_emitted = 0
        self.is_running = True

        if not self.jobs:
            self.error_found.emit("This profile has no jobs. Nothing to back up.")
            self.finished.emit(0, 0, 0)
            return

        self.progress_update.emit("--- SCANNING: Calculating job size... ---")
        scan_started = time.time()

        for job in self.jobs:
            if not self.is_running:
                break
            src = os.path.abspath(job['src'].rstrip(os.sep))
            base_folder = os.path.basename(src)
            target_root = os.path.join(os.path.abspath(job['dst']), base_folder)

            if not os.path.isdir(src):
                self._report_error(f"SOURCE MISSING: {src}", self.scan_errors)
                continue
            if not os.path.isdir(job['dst']):
                self._report_error(f"DESTINATION MISSING: {job['dst']}", self.scan_errors)
                continue

            self.progress_update.emit(f"Scanning: {src}")
            self._walk(src, target_root, os.path.abspath(job['dst']))

        scan_seconds = time.time() - scan_started

        if not self.is_running:
            self.progress_update.emit("--- STOPPED during scan. ---")
            self.finished.emit(0, 0, 0)
            return

        self.progress_update.emit(
            f"Scan finished in {scan_seconds:,.0f}s: {self.entries_seen:,} items examined, "
            f"{len(self.task_list):,} to copy ({self._human(self.total_bytes)}), "
            f"{len(self.scan_errors):,} errors."
        )

        self.task_stats_ready.emit(len(self.task_list), self.total_bytes)

        if not self.task_list:
            if self.scan_errors:
                self.progress_update.emit(
                    "--- NOTHING COPIED: the scan failed before it found any files. "
                    "Fix the errors above and run again. ---")
            else:
                self.progress_update.emit(
                    "--- NOTHING TO COPY: the destination is already up to date. ---")
            self.finished.emit(0, 0, 0)
            return

        # ------------------------------------------------------------ copying
        copied = 0
        failed = 0
        for index, task in enumerate(self.task_list, start=1):
            if not self.is_running:
                break
            self.progress_update.emit(
                f"[{index:,}/{len(self.task_list):,}] {os.path.basename(task['src'])}")
            if self.unstoppable_copy(task['src'], task['dst']):
                copied += 1
            else:
                failed += 1

        # ------------------------------------------------------------ cleanup
        if self.backup_mode == "Exact Sync (Mirror)" and self.is_running:
            if self.scan_errors:
                self.progress_update.emit(
                    "--- SYNC CLEANUP SKIPPED: the scan had errors, so the source "
                    "listing is incomplete. Deleting from the destination now could "
                    "remove good backups. ---")
            elif failed:
                self.progress_update.emit(
                    "--- SYNC CLEANUP SKIPPED: some files failed to copy. ---")
            else:
                self.progress_update.emit("--- SYNC: Cleaning up destination... ---")
                self._sync_cleanup()

        self.progress_update.emit(
            f"--- SUMMARY: {copied:,} copied, {failed:,} failed, "
            f"{self.in_use_count:,} in use, {len(self.scan_errors):,} scan errors. ---")

        self.finished.emit(len(self.task_list), copied, self.in_use_count)

    # ------------------------------------------------------------------ scan

    def _scandir_with_retry(self, path):
        """Returns a list of entries, or None if the folder could not be read."""
        delay = 1
        for attempt in range(self.retries + 1):
            if not self.is_running:
                return None
            try:
                with os.scandir(path) as it:
                    return list(it)
            except OSError as e:
                if e.errno in (errno.EACCES, errno.EPERM, errno.ENOENT, errno.ENOTDIR):
                    # Not transient - retrying will not help.
                    self._report_error(f"FOLDER SKIPPED: {path} - {e.strerror}", self.scan_errors)
                    return None
                if attempt < self.retries:
                    self.progress_update.emit(
                        f"Folder unreadable ({e.strerror}), retry "
                        f"{attempt + 1}/{self.retries}: {path}")
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                else:
                    self._report_error(
                        f"FOLDER FAILED after {self.retries} retries: {path} - {e.strerror}",
                        self.scan_errors)
                    return None
        return None

    def _walk(self, src_root, dst_root, dst_base=None):
        """Iterative walk. One bad entry can no longer abandon a whole folder."""
        visited = set()
        stack = [(src_root, dst_root)]

        # Never descend into the destination. If the destination lives inside
        # the source, walking into it means scanning the backup of the backup
        # of the backup, which never ends.
        forbidden = []
        for candidate in (dst_base, dst_root):
            if candidate:
                forbidden.append(os.path.abspath(candidate).rstrip(os.sep))

        while stack:
            if not self.is_running:
                return
            current_src, current_dst = stack.pop()

            here = os.path.abspath(current_src).rstrip(os.sep)
            if any(here == f or here.startswith(f + os.sep) for f in forbidden):
                self.progress_update.emit(
                    f"Skipping destination folder found inside the source: {current_src}")
                continue

            try:
                st = os.stat(current_src)
                key = (st.st_dev, st.st_ino)
                if key in visited:
                    self.progress_update.emit(f"Symlink loop skipped: {current_src}")
                    continue
                visited.add(key)
            except OSError as e:
                self._report_error(f"FOLDER SKIPPED: {current_src} - {e.strerror}",
                                   self.scan_errors)
                continue

            entries = self._scandir_with_retry(current_src)
            if entries is None:
                continue

            for entry in entries:
                if not self.is_running:
                    return
                self.entries_seen += 1
                if self.entries_seen % self.SCAN_REPORT_EVERY == 0:
                    self.progress_update.emit(
                        f"Scanning... {self.entries_seen:,} items examined, "
                        f"{len(self.task_list):,} queued "
                        f"({self._human(self.total_bytes)})")

                target_path = os.path.join(current_dst, entry.name)
                try:
                    if entry.is_symlink() and not self.follow_links:
                        continue

                    if self.backup_mode == "Exact Sync (Mirror)":
                        self.source_structure.add(target_path)

                    if entry.is_dir():
                        stack.append((entry.path, target_path))
                    elif entry.is_file():
                        if self._check_if_needed(entry, target_path):
                            size = entry.stat().st_size
                            self.task_list.append({
                                'src': entry.path,
                                'dst': target_path,
                                'size': size,
                            })
                            self.total_bytes += size
                except OSError as e:
                    # Isolated to this one entry. The rest of the folder continues.
                    self._report_error(f"SKIPPED: {entry.path} - {e.strerror}",
                                       self.scan_errors)

    def _check_if_needed(self, entry, dst):
        if self.backup_mode == "Full Overwrite":
            return True
        try:
            d = os.stat(dst)
        except FileNotFoundError:
            return True
        except OSError as e:
            self._report_error(f"DESTINATION UNREADABLE, will re-copy: {dst} - {e.strerror}",
                               self.scan_errors)
            return True

        s = entry.stat()
        if s.st_size != d.st_size:
            return True
        if abs(s.st_mtime - d.st_mtime) > self.mtime_tolerance:
            return True
        if self.deep_verify and not self._quick_hash_matches(entry.path, dst):
            return True
        return False

    def _quick_hash_matches(self, src, dst):
        import hashlib

        def quick(path):
            try:
                with open(path, 'rb') as f:
                    head = f.read(1024 * 1024)
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    tail = b""
                    if size > 1024 * 1024:
                        f.seek(size - 1024 * 1024)
                        tail = f.read(1024 * 1024)
                    return hashlib.md5(head + tail).hexdigest()
            except OSError:
                return None

        a, b = quick(src), quick(dst)
        return a is not None and a == b

    # ------------------------------------------------------------------ copy

    def unstoppable_copy(self, src, dst):
        if not self.is_running:
            return False

        try:
            expected = os.path.getsize(src)
        except OSError as e:
            self._report_error(f"Cannot stat {src} - {e.strerror}", self.copy_errors)
            return False

        part = dst + self.PART_SUFFIX
        written = 0
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
        except OSError as e:
            self._report_error(f"Cannot create folder for {dst} - {e.strerror}", self.copy_errors)
            return False

        try:
            with open(src, 'rb') as f_in, open(part, 'wb') as f_out:
                while self.is_running:
                    chunk = None
                    read_failed = False
                    delay = 1
                    for attempt in range(self.retries + 1):
                        if not self.is_running:
                            break
                        try:
                            chunk = f_in.read(self.buffer_size)
                            break
                        except OSError as e:
                            if e.errno in (errno.EBUSY, errno.ETXTBSY):
                                self.in_use_count += 1
                                read_failed = True
                                break
                            if attempt < self.retries:
                                time.sleep(delay)
                                delay = min(delay * 2, 30)
                            else:
                                read_failed = True
                    if read_failed:
                        self._cleanup_part(part)
                        self._report_error(
                            f"Unreadable, skipped: {src}", self.copy_errors)
                        return False
                    if not chunk:
                        break
                    f_out.write(chunk)
                    written += len(chunk)
                    self.chunk_finished.emit(len(chunk))
                f_out.flush()
                os.fsync(f_out.fileno())

            if not self.is_running:
                self._cleanup_part(part)
                return False

            if written != expected:
                self._cleanup_part(part)
                self._report_error(
                    f"Short copy ({written:,} of {expected:,} bytes), not kept: {src}",
                    self.copy_errors)
                return False

            s_stat = os.stat(src)
            os.utime(part, (s_stat.st_atime, s_stat.st_mtime))
            os.replace(part, dst)
            return True

        except Exception as e:
            self._cleanup_part(part)
            self._report_error(f"Error copying {os.path.basename(src)}: {e}", self.copy_errors)
            return False

    @staticmethod
    def _cleanup_part(part):
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass

    # --------------------------------------------------------------- cleanup

    def _sync_cleanup(self):
        removed = 0
        for job in self.jobs:
            if not self.is_running:
                break
            base_folder = os.path.basename(os.path.abspath(job['src'].rstrip(os.sep)))
            target_root = os.path.join(os.path.abspath(job['dst']), base_folder)
            if not os.path.isdir(target_root):
                continue

            for root, dirs, files in os.walk(target_root, topdown=False):
                if not self.is_running:
                    break
                for name in files + dirs:
                    if not self.is_running:
                        break
                    full_path = os.path.join(root, name)
                    if name.endswith(self.PART_SUFFIX):
                        self._cleanup_part(full_path)
                        continue
                    if full_path not in self.source_structure:
                        try:
                            if os.path.isfile(full_path):
                                os.remove(full_path)
                                removed += 1
                            elif os.path.isdir(full_path):
                                os.rmdir(full_path)
                                removed += 1
                        except OSError:
                            pass
        self.progress_update.emit(f"--- SYNC: removed {removed:,} extra items. ---")
