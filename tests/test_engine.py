import os
import shutil
import sys
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

import engine
import index
from engine import (MODE_MIRROR, MODE_OVERWRITE, PART_SUFFIX, VERSION_KEEP,
                    VERSIONS_DIR, BackupWorker)

APP = QApplication.instance() or QApplication([])


def write(path, data=b"x" * 512):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def tree(root):
    out = {}
    for base, _, files in os.walk(root):
        for f in files:
            p = os.path.join(base, f)
            out[os.path.relpath(p, root)] = os.path.getsize(p)
    return out


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="milback-test-")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state, exist_ok=True)
        self._real_state = index.STATE_DIR
        index.STATE_DIR = self.state
        import runlog
        self._real_log = runlog.LOG_DIR
        runlog.LOG_DIR = os.path.join(self.state, "logs")
        runlog.STATUS_FILE = os.path.join(self.state, "last_runs.json")
        self.src = os.path.join(self.tmp, "src", "Data")
        self.dst = os.path.join(self.tmp, "dst")
        os.makedirs(self.src, exist_ok=True)
        os.makedirs(self.dst, exist_ok=True)

    def tearDown(self):
        index.STATE_DIR = self._real_state
        import runlog
        runlog.LOG_DIR = self._real_log
        shutil.rmtree(self.tmp, ignore_errors=True)

    def backup(self, timeout_ms=30000, **settings):
        settings.setdefault("jobs", [{"src": self.src, "dst": self.dst}])
        settings.setdefault("workers", 3)
        worker = BackupWorker(settings, "test")
        logs, errors, result = [], [], {}
        worker.progress_update.connect(logs.append)
        worker.error_found.connect(errors.append)
        loop = QEventLoop()

        def done(found, copied, in_use):
            result.update(found=found, copied=copied, in_use=in_use)
            loop.quit()

        worker.finished.connect(done)
        guard = QTimer()
        guard.setSingleShot(True)
        guard.timeout.connect(lambda: (result.update(timeout=True), loop.quit()))
        guard.start(timeout_ms)
        worker.start()
        loop.exec()
        worker.stop()
        worker.wait(5000)
        self.assertNotIn("timeout", result, "worker did not finish")
        return result, logs, errors, worker

    def backup_dir(self):
        return os.path.join(self.dst, os.path.basename(self.src))


class TestCopying(EngineCase):
    def test_copies_everything(self):
        write(os.path.join(self.src, "a.txt"))
        write(os.path.join(self.src, "sub", "b.txt"), b"y" * 4096)
        result, _, errors, _ = self.backup()
        self.assertEqual(result["copied"], 2)
        self.assertEqual(errors, [])
        self.assertEqual(tree(self.backup_dir()),
                         {"a.txt": 512, os.path.join("sub", "b.txt"): 4096})

    def test_no_part_files_survive(self):
        write(os.path.join(self.src, "a.txt"))
        self.backup()
        leftovers = [f for f in tree(self.backup_dir()) if f.endswith(PART_SUFFIX)]
        self.assertEqual(leftovers, [])

    def test_second_run_copies_nothing(self):
        write(os.path.join(self.src, "a.txt"))
        self.backup()
        result, _, _, _ = self.backup()
        self.assertEqual(result["copied"], 0)

    def test_changed_file_is_recopied(self):
        path = write(os.path.join(self.src, "a.txt"))
        self.backup()
        time.sleep(0.01)
        write(path, b"z" * 2048)
        result, _, _, _ = self.backup()
        self.assertEqual(result["copied"], 1)
        self.assertEqual(tree(self.backup_dir())["a.txt"], 2048)

    def test_coarse_timestamps_do_not_force_recopy(self):
        write(os.path.join(self.src, "a.txt"))
        self.backup()
        target = os.path.join(self.backup_dir(), "a.txt")
        st = os.stat(os.path.join(self.src, "a.txt"))
        os.utime(target, (st.st_atime, st.st_mtime + 1.4))
        result, _, _, _ = self.backup()
        self.assertEqual(result["copied"], 0)

    def test_full_overwrite_recopies(self):
        write(os.path.join(self.src, "a.txt"))
        self.backup()
        result, _, _, _ = self.backup(backup_mode=MODE_OVERWRITE)
        self.assertEqual(result["copied"], 1)

    def test_parallel_transfers(self):
        for i in range(25):
            write(os.path.join(self.src, f"f{i}.bin"), os.urandom(20000))
        result, _, errors, _ = self.backup(workers=8)
        self.assertEqual(result["copied"], 25)
        self.assertEqual(errors, [])
        self.assertEqual(len(tree(self.backup_dir())), 25)

    def test_resume_of_partial_transfer(self):
        data = os.urandom(50000)
        src = write(os.path.join(self.src, "big.bin"), data)
        target = os.path.join(self.backup_dir(), "big.bin")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target + PART_SUFFIX, "wb") as f:
            f.write(data[:20000])
        os.utime(target + PART_SUFFIX, (time.time() + 5, time.time() + 5))
        self.backup()
        with open(target, "rb") as f:
            self.assertEqual(f.read(), data)

    def test_stale_partial_is_discarded(self):
        data = os.urandom(40000)
        write(os.path.join(self.src, "big.bin"), data)
        target = os.path.join(self.backup_dir(), "big.bin")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target + PART_SUFFIX, "wb") as f:
            f.write(b"\x00" * 20000)
        os.utime(target + PART_SUFFIX, (1, 1))
        self.backup()
        with open(target, "rb") as f:
            self.assertEqual(f.read(), data)


class TestScanResilience(EngineCase):
    def test_bad_entry_does_not_lose_the_folder(self):
        for i in range(60):
            write(os.path.join(self.src, f"f{i:02d}.txt"))
        bad = os.path.join(self.src, "f30.txt")
        os.remove(bad)
        os.symlink("/proc/self/mem", bad)
        result, _, _, _ = self.backup(retries=0)
        self.assertGreaterEqual(len(tree(self.backup_dir())), 59)

    def test_symlink_loop_terminates(self):
        write(os.path.join(self.src, "real.txt"))
        os.makedirs(os.path.join(self.src, "sub"), exist_ok=True)
        os.symlink(self.src, os.path.join(self.src, "sub", "loop"))
        result, logs, _, _ = self.backup(timeout_ms=20000)
        self.assertEqual(result["copied"], 1)
        self.assertTrue(any("loop" in line.lower() for line in logs))

    def test_destination_inside_source_is_skipped(self):
        write(os.path.join(self.src, "a.txt"))
        nested = os.path.join(self.src, "backup")
        os.makedirs(nested, exist_ok=True)
        result, logs, _, _ = self.backup(
            jobs=[{"src": self.src, "dst": nested}], timeout_ms=20000)
        self.assertTrue(any("destination" in line.lower() for line in logs))

    def test_missing_source_is_reported(self):
        _, _, errors, _ = self.backup(
            jobs=[{"src": os.path.join(self.tmp, "nope"), "dst": self.dst}])
        self.assertTrue(any("SOURCE MISSING" in e for e in errors))

    def test_empty_profile_is_reported(self):
        _, _, errors, _ = self.backup(jobs=[])
        self.assertTrue(any("no jobs" in e for e in errors))


class TestMirrorSafety(EngineCase):
    def test_unmounted_source_does_not_wipe_backup(self):
        precious = write(os.path.join(self.backup_dir(), "precious.txt"))
        result, _, errors, _ = self.backup(backup_mode=MODE_MIRROR)
        self.assertTrue(os.path.exists(precious))
        self.assertTrue(any("scanned as empty" in e for e in errors))

    def test_large_deletion_is_refused(self):
        write(os.path.join(self.src, "keep.txt"))
        for i in range(20):
            write(os.path.join(self.backup_dir(), f"old{i}.txt"))
        result, _, errors, _ = self.backup(backup_mode=MODE_MIRROR)
        self.assertTrue(any("CLEANUP REFUSED" in e for e in errors))
        self.assertEqual(len(tree(self.backup_dir())), 21)

    def test_large_deletion_allowed_when_overridden(self):
        write(os.path.join(self.src, "keep.txt"))
        for i in range(20):
            write(os.path.join(self.backup_dir(), f"old{i}.txt"))
        self.backup(backup_mode=MODE_MIRROR, allow_large_deletes=True)
        self.assertEqual(sorted(tree(self.backup_dir())), ["keep.txt"])

    def test_small_deletion_proceeds(self):
        for i in range(20):
            write(os.path.join(self.src, f"keep{i}.txt"))
        write(os.path.join(self.backup_dir(), "stale.txt"))
        self.backup(backup_mode=MODE_MIRROR)
        self.assertNotIn("stale.txt", tree(self.backup_dir()))
        self.assertEqual(len(tree(self.backup_dir())), 20)

    def test_cleanup_skipped_after_scan_errors(self):
        for i in range(20):
            write(os.path.join(self.src, f"keep{i}.txt"))
        precious = write(os.path.join(self.backup_dir(), "precious.txt"))
        unreadable = os.path.join(self.src, "locked")
        os.makedirs(unreadable, exist_ok=True)

        worker = BackupWorker({"jobs": [{"src": self.src, "dst": self.dst}],
                               "backup_mode": MODE_MIRROR, "workers": 2}, "test")
        real_listdir = worker._listdir

        def flaky(path):
            if path == unreadable:
                worker._fail(f"FOLDER FAILED: {path}", worker.scan_errors)
                return None
            return real_listdir(path)

        worker._listdir = flaky
        logs = []
        worker.progress_update.connect(logs.append)
        loop = QEventLoop()
        worker.finished.connect(lambda *a: loop.quit())
        guard = QTimer(); guard.setSingleShot(True)
        guard.timeout.connect(loop.quit); guard.start(30000)
        worker.start(); loop.exec(); worker.wait(5000)

        self.assertTrue(os.path.exists(precious),
                        "a job whose scan failed must not delete from the backup")
        self.assertTrue(any("CLEANUP SKIPPED" in line for line in logs))


class TestDryRun(EngineCase):
    def test_dry_run_writes_nothing(self):
        write(os.path.join(self.src, "a.txt"))
        result, logs, _, _ = self.backup(dry_run=True)
        self.assertEqual(result["copied"], 1)
        self.assertFalse(os.path.exists(self.backup_dir()))
        self.assertTrue(any("would copy" in line for line in logs))

    def test_dry_run_does_not_delete(self):
        write(os.path.join(self.src, "keep.txt"))
        stale = write(os.path.join(self.backup_dir(), "stale.txt"))
        self.backup(dry_run=True, backup_mode=MODE_MIRROR,
                    allow_large_deletes=True)
        self.assertTrue(os.path.exists(stale))


class TestVersioning(EngineCase):
    def test_previous_version_is_kept(self):
        path = write(os.path.join(self.src, "a.txt"), b"first")
        self.backup(versioning=VERSION_KEEP)
        time.sleep(0.01)
        write(path, b"second-and-longer")
        self.backup(versioning=VERSION_KEEP)
        with open(os.path.join(self.backup_dir(), "a.txt"), "rb") as f:
            self.assertEqual(f.read(), b"second-and-longer")
        kept = []
        for base, _, files in os.walk(self.dst):
            if VERSIONS_DIR in base.split(os.sep):
                kept.extend(os.path.join(base, f) for f in files)
        self.assertEqual(len(kept), 1)
        with open(kept[0], "rb") as f:
            self.assertEqual(f.read(), b"first")


class TestIndex(EngineCase):
    def test_trusted_index_skips_destination(self):
        write(os.path.join(self.src, "a.txt"))
        self.backup(trust_index=True)
        result, _, _, worker = self.backup(trust_index=True)
        self.assertEqual(result["copied"], 0)
        self.assertEqual(worker.c.skipped, 1)

    def test_index_records_survive_between_runs(self):
        write(os.path.join(self.src, "a.txt"))
        self.backup(trust_index=True)
        db = index.StateIndex()
        key = index.job_key(self.src, self.dst)
        self.assertEqual(len(db.load_job(key)), 1)
        db.close()


class TestVerification(EngineCase):
    def test_deep_verify_detects_silent_corruption(self):
        src = write(os.path.join(self.src, "a.txt"), b"A" * 4096)
        self.backup()
        target = os.path.join(self.backup_dir(), "a.txt")
        st = os.stat(target)
        with open(target, "r+b") as f:
            f.seek(2048)
            f.write(b"B" * 16)
        os.utime(target, (st.st_atime, st.st_mtime))
        unchecked, _, _, _ = self.backup()
        self.assertEqual(unchecked["copied"], 0)
        checked, _, _, _ = self.backup(deep_verify=True)
        self.assertEqual(checked["copied"], 1)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"A" * 4096)

    def test_quick_verify_misses_middle_corruption(self):
        write(os.path.join(self.src, "a.txt"), b"A" * (3 * 1024 * 1024))
        self.backup()
        target = os.path.join(self.backup_dir(), "a.txt")
        st = os.stat(target)
        with open(target, "r+b") as f:
            f.seek(1500000)
            f.write(b"B" * 16)
        os.utime(target, (st.st_atime, st.st_mtime))
        quick, _, _, _ = self.backup(deep_verify=True, quick_verify=True)
        self.assertEqual(quick["copied"], 0)
        full, _, _, _ = self.backup(deep_verify=True)
        self.assertEqual(full["copied"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
