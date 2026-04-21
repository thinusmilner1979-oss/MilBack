import os
import time
import hashlib

class ResilientWalker:
    def __init__(self, settings):
        self.sources = settings.get('sources', [])
        self.destination = settings.get('destination', "")
        self.deep_verify = settings.get('deep_verify', False)
        self.follow_links = settings.get('follow_links', True)
        
        # Statistics and Error Logs
        self.task_list = []
        self.errors = []
        self.total_size_to_copy = 0

    def get_quick_hash(self, filepath):
        """Reads first and last 1MB to verify file integrity without full scan."""
        try:
            with open(filepath, 'rb') as f:
                first_mb = f.read(1024 * 1024)
                f.seek(0, os.SEEK_END)
                # If file is smaller than 1MB, seek will handle it
                filesize = f.tell()
                if filesize > 1024 * 1024:
                    f.seek(filesize - 1024 * 1024)
                last_mb = f.read(1024 * 1024)
                return hashlib.md5(first_mb + last_mb).hexdigest()
        except Exception as e:
            return None

    def should_copy(self, src_file, dst_file):
        """Smart comparison logic."""
        if not os.path.exists(dst_file):
            return True # New file
            
        src_stat = os.stat(src_file)
        dst_stat = os.stat(dst_file)

        # Check Metadata (Size and Modified Time)
        if src_stat.st_size != dst_stat.st_size:
            return True
        if int(src_stat.st_mtime) != int(dst_stat.st_mtime):
            return True

        # Optional Deep Verify
        if self.deep_verify:
            if self.get_quick_hash(src_file) != self.get_quick_hash(dst_file):
                return True

        return False # Files appear identical

    def scan(self):
        """The main loop that walks the directories."""
        for source_root in self.sources:
            self._unstopable_walk(source_root)
        
        return self.task_list, self.errors

    def _unstopable_walk(self, current_dir):
        """Internal recursive walker with error shielding."""
        try:
            # scandir is much faster than os.walk for metadata
            with os.scandir(current_dir) as it:
                for entry in it:
                    try:
                        # 1. Handle Symlinks
                        if entry.is_symlink() and not self.follow_links:
                            continue # Skip links if user doesn't want them

                        # 2. Handle Directories (Recurse)
                        if entry.is_dir():
                            self._unstopable_walk(entry.path)
                        
                        # 3. Handle Files
                        elif entry.is_file():
                            # Calculate where this file should live in the destination
                            # (We'll refine path mapping in the next step)
                            rel_path = os.path.relpath(entry.path, start=os.path.dirname(current_dir))
                            target_path = os.path.join(self.destination, rel_path)

                            if self.should_copy(entry.path, target_path):
                                task = {
                                    'src': entry.path,
                                    'dst': target_path,
                                    'size': entry.stat().st_size,
                                    'mtime': entry.stat().st_mtime
                                }
                                self.task_list.append(task)
                                self.total_size_to_copy += task['size']

                    except OSError as e:
                        self.errors.append(f"FILE ERROR: {entry.path} - {str(e)}")
                        continue

        except OSError as e:
            self.errors.append(f"FOLDER ERROR: {current_dir} - {str(e)}")
