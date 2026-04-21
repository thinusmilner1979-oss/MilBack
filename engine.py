import os
import time
from PyQt6.QtCore import QThread, pyqtSignal

class BackupWorker(QThread):
    progress_update = pyqtSignal(str)
    error_found = pyqtSignal(str)
    
    # FIXED: Using 'object' prevents the 2.14 GB overflow limit
    task_stats_ready = pyqtSignal(int, object) 
    chunk_finished = pyqtSignal(object)        
    
    finished = pyqtSignal(int, int, int)

    def __init__(self, settings):
        super().__init__()
        self.jobs = settings.get('jobs', [])
        self.deep_verify = settings.get('deep_verify', False)
        self.retries = settings.get('retries', 5)
        self.wait_timeout = settings.get('wait_timeout', 1800)
        self.backup_mode = settings.get('backup_mode', 'Add & Update (Incremental)')
        
        self.buffer_size = 64 * 1024 
        self.task_list = []
        self.total_bytes = 0
        self.source_structure = set()
        self.in_use_count = 0
        self.is_running = True

    def stop(self):
        self.is_running = False

    def unstoppable_copy(self, src, dst):
        if not self.is_running: return False
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            with open(src, 'rb') as f_in, open(dst, 'wb') as f_out:
                while self.is_running:
                    chunk = None
                    for attempt in range(self.retries + 1):
                        if not self.is_running: break
                        try:
                            chunk = f_in.read(self.buffer_size)
                            break 
                        except OSError as e:
                            if e.errno == 16: # EBUSY
                                self.in_use_count += 1
                                return False
                            time.sleep(1)
                    
                    if not chunk or not self.is_running: break
                    f_out.write(chunk)
                    self.chunk_finished.emit(len(chunk))
            
            if not self.is_running:
                if os.path.exists(dst): os.remove(dst)
                return False

            s_stat = os.stat(src)
            os.utime(dst, (s_stat.st_atime, s_stat.st_mtime))
            return True
        except Exception as e:
            self.error_found.emit(f"Error copying {os.path.basename(src)}: {str(e)}")
            return False

    def run(self):
        self.in_use_count = 0
        self.total_bytes = 0
        self.task_list = []
        self.source_structure.clear()
        self.is_running = True

        self.progress_update.emit("--- SCANNING: Calculating job size... ---")
        
        # Phase 1: Scan all jobs
        for job in self.jobs:
            if not self.is_running: break
            src = job['src'].rstrip(os.sep)
            base_folder = os.path.basename(src)
            target_root = os.path.join(job['dst'], base_folder)
            self._walk(src, target_root)

        if not self.is_running:
            self.finished.emit(0, 0, 0)
            return

        self.task_stats_ready.emit(len(self.task_list), self.total_bytes)
        time.sleep(0.1)

        # Phase 2: Copy
        copied = 0
        for task in self.task_list:
            if not self.is_running: break
            self.progress_update.emit(f"Backing up: {os.path.basename(task['src'])}")
            if self.unstoppable_copy(task['src'], task['dst']):
                copied += 1
        
        # Phase 3: Sync Cleanup (Only runs if "Exact Sync (Mirror)" is selected)
        if self.backup_mode == "Exact Sync (Mirror)" and self.is_running:
            self.progress_update.emit("--- SYNC: Cleaning up destination... ---")
            self._sync_cleanup()

        self.finished.emit(len(self.task_list), copied, self.in_use_count)

    def _walk(self, current_src, current_dst):
        try:
            with os.scandir(current_src) as it:
                for entry in it:
                    if not self.is_running: break
                    target_path = os.path.join(current_dst, entry.name)
                    
                    # Track for Sync cleanup
                    if self.backup_mode == "Exact Sync (Mirror)": 
                        self.source_structure.add(target_path)
                    
                    if entry.is_dir():
                        self._walk(entry.path, target_path)
                    elif entry.is_file():
                        if self._check_if_needed(entry.path, target_path):
                            size = entry.stat().st_size
                            self.task_list.append({'src': entry.path, 'dst': target_path, 'size': size})
                            self.total_bytes += size
        except Exception: 
            pass

    def _check_if_needed(self, src, dst):
        """Logic for Overwrite vs Incremental comparison."""
        if self.backup_mode == "Full Overwrite":
            return True
        
        # Incremental & Sync logic: only copy if missing or changed
        if not os.path.exists(dst): 
            return True
        
        s, d = os.stat(src), os.stat(dst)
        if s.st_size != d.st_size or int(s.st_mtime) != int(d.st_mtime):
            return True
            
        return False

    def _sync_cleanup(self):
        for job in self.jobs:
            if not self.is_running: break
            base_folder = os.path.basename(job['src'].rstrip(os.sep))
            target_root = os.path.join(job['dst'], base_folder)
            
            for root, dirs, files in os.walk(target_root, topdown=False):
                if not self.is_running: break
                for name in files + dirs:
                    if not self.is_running: break
                    full_path = os.path.join(root, name)
                    if full_path not in self.source_structure:
                        try:
                            if os.path.isfile(full_path): os.remove(full_path)
                            elif os.path.isdir(full_path): os.rmdir(full_path)
                        except: pass
