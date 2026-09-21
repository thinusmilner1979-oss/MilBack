import os
import sys
import time

from PyQt6.QtCore import QTime, QTimer
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox,
                             QFileDialog, QGroupBox, QHBoxLayout, QHeaderView,
                             QInputDialog, QLabel, QListWidget, QMainWindow, QMenu,
                             QMessageBox, QProgressBar, QPushButton, QSpinBox,
                             QStyle, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
                             QTextEdit, QTimeEdit, QVBoxLayout, QWidget)

import profiles as profile_store
from engine import (MODE_INCREMENTAL, MODE_MIRROR, MODE_OVERWRITE, VERSION_KEEP,
                    VERSION_NONE, VERSION_SNAPSHOT, BackupWorker, human)
from runlog import load_status

LOG_LINE_LIMIT = 2000


class MilBackWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MilBack v1.1.0")
        self.resize(1100, 880)

        self.profiles = {}
        self.current_profile_name = None
        self.worker = None
        self.total_bytes = 0.0
        self.bytes_copied = 0
        self.start_time = time.time()
        self._loading = False

        self._build_tray()
        self._build_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_schedule)
        self.timer.start(60000)

        self.load_all_profiles()

    def _build_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DriveNetIcon))
        menu = QMenu()
        show = QAction("Show Dashboard", self)
        show.triggered.connect(self.show_normal)
        quit_action = QAction("Exit MilBack", self)
        quit_action.triggered.connect(self.force_quit)
        menu.addAction(show)
        menu.addAction(quit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(
            lambda reason: self.show_normal()
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray_icon.show()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)

        sidebar = QVBoxLayout()
        sidebar.addWidget(QLabel("<b>Profiles:</b>"))
        self.profile_list = QListWidget()
        self.profile_list.currentItemChanged.connect(self.on_profile_changed)
        sidebar.addWidget(self.profile_list)
        buttons = QHBoxLayout()
        add = QPushButton("New")
        add.clicked.connect(self.new_profile)
        remove = QPushButton("Delete")
        remove.clicked.connect(self.delete_profile)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        sidebar.addLayout(buttons)
        layout.addLayout(sidebar, 1)

        self.settings_pane = QWidget()
        self.settings_pane.setEnabled(False)
        pane = QVBoxLayout(self.settings_pane)

        self.profile_title = QLabel("Select a Profile to begin")
        self.profile_title.setStyleSheet(
            "font-size: 16px; font-weight: bold; color: #2e7d32;")
        pane.addWidget(self.profile_title)
        self.last_run_label = QLabel("")
        self.last_run_label.setStyleSheet("color: #888;")
        pane.addWidget(self.last_run_label)

        pane.addWidget(QLabel("<b>Backup Jobs (Source -> Destination):</b>"))
        self.job_table = QTableWidget(0, 2)
        self.job_table.setHorizontalHeaderLabels(["Source Folder", "Destination Folder"])
        self.job_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        pane.addWidget(self.job_table)

        job_buttons = QHBoxLayout()
        add_job = QPushButton("Add Job")
        add_job.clicked.connect(self.add_job)
        remove_job = QPushButton("Remove Selected Job")
        remove_job.clicked.connect(self.remove_job)
        job_buttons.addWidget(add_job)
        job_buttons.addWidget(remove_job)
        pane.addLayout(job_buttons)

        pane.addWidget(self._options_group())
        pane.addWidget(self._safety_group())
        pane.addWidget(self._schedule_group())

        self.save_btn = QPushButton("SAVE PROFILE SETTINGS")
        self.save_btn.setFixedHeight(40)
        self.save_btn.setStyleSheet(
            "background-color: #0d47a1; color: white; font-weight: bold;")
        self.save_btn.clicked.connect(self.save_current_profile)
        pane.addWidget(self.save_btn)

        run_row = QHBoxLayout()
        self.start_btn = QPushButton("START BACKUP")
        self.start_btn.setFixedHeight(50)
        self.start_btn.setStyleSheet(
            "background-color: #1b5e20; color: white; font-weight: bold;")
        self.start_btn.clicked.connect(lambda: self.start_backup(dry_run=False))
        self.dry_btn = QPushButton("DRY RUN")
        self.dry_btn.setFixedHeight(50)
        self.dry_btn.setStyleSheet(
            "background-color: #37474f; color: white; font-weight: bold;")
        self.dry_btn.clicked.connect(lambda: self.start_backup(dry_run=True))
        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setFixedHeight(50)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet(
            "background-color: #b71c1c; color: white; font-weight: bold;")
        self.stop_btn.clicked.connect(self.stop_backup)
        run_row.addWidget(self.start_btn, 3)
        run_row.addWidget(self.dry_btn, 2)
        run_row.addWidget(self.stop_btn, 1)
        pane.addLayout(run_row)

        self.stats_label = QLabel("Ready.")
        pane.addWidget(self.stats_label)
        self.progress_bar = QProgressBar()
        pane.addWidget(self.progress_bar)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.document().setMaximumBlockCount(LOG_LINE_LIMIT)
        self.log.setStyleSheet(
            "background: #000; color: #33ff33; font-family: monospace;")
        pane.addWidget(self.log)

        layout.addWidget(self.settings_pane, 3)

    def _options_group(self):
        group = QGroupBox("Backup Options")
        box = QVBoxLayout()

        row = QHBoxLayout()
        row.addWidget(QLabel("Backup Type:"))
        self.backup_mode_combo = QComboBox()
        self.backup_mode_combo.addItems([MODE_INCREMENTAL, MODE_MIRROR, MODE_OVERWRITE])
        row.addWidget(self.backup_mode_combo)
        row.addSpacing(20)
        row.addWidget(QLabel("Parallel transfers:"))
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 32)
        self.workers_spin.setValue(4)
        self.workers_spin.setToolTip(
            "Higher values hide network latency on a share. Lower values are "
            "better when the destination is a single spinning disk.")
        row.addWidget(self.workers_spin)
        row.addStretch()
        box.addLayout(row)

        self.deep_verify_check = QCheckBox(
            "Deep Verify: compare file contents, not just size and timestamp")
        self.quick_verify_check = QCheckBox(
            "   ... sample only the first and last MB (faster, misses middle corruption)")
        box.addWidget(self.deep_verify_check)
        box.addWidget(self.quick_verify_check)
        self.deep_verify_check.toggled.connect(self.quick_verify_check.setEnabled)
        self.quick_verify_check.setEnabled(False)

        self.trust_index_check = QCheckBox(
            "Fast incremental: trust the local index and skip checking the destination")
        self.trust_index_check.setToolTip(
            "Much faster over a slow share. The destination is re-checked "
            "periodically in case it was changed outside MilBack.")
        box.addWidget(self.trust_index_check)

        row = QHBoxLayout()
        row.addWidget(QLabel("Block Retries:"))
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(0, 50)
        self.retry_spin.setValue(5)
        row.addWidget(self.retry_spin)
        row.addWidget(QLabel("Wait Window (mins):"))
        self.wait_spin = QSpinBox()
        self.wait_spin.setRange(1, 1440)
        self.wait_spin.setValue(30)
        row.addWidget(self.wait_spin)
        row.addStretch()
        box.addLayout(row)

        group.setLayout(box)
        return group

    def _safety_group(self):
        group = QGroupBox("Safety and History")
        box = QVBoxLayout()

        row = QHBoxLayout()
        row.addWidget(QLabel("When a file is replaced or removed:"))
        self.versioning_combo = QComboBox()
        self.versioning_combo.addItems([VERSION_NONE, VERSION_KEEP, VERSION_SNAPSHOT])
        row.addWidget(self.versioning_combo)
        row.addWidget(QLabel("Keep for (days):"))
        self.retention_spin = QSpinBox()
        self.retention_spin.setRange(0, 3650)
        self.retention_spin.setValue(30)
        row.addWidget(self.retention_spin)
        row.addStretch()
        box.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("Mirror mode: refuse to delete more than"))
        self.delete_ratio_spin = QDoubleSpinBox()
        self.delete_ratio_spin.setRange(0.01, 1.0)
        self.delete_ratio_spin.setSingleStep(0.05)
        self.delete_ratio_spin.setDecimals(2)
        self.delete_ratio_spin.setValue(0.20)
        row.addWidget(self.delete_ratio_spin)
        row.addWidget(QLabel("of the backup in one run"))
        row.addStretch()
        box.addLayout(row)

        self.allow_large_deletes_check = QCheckBox(
            "Allow large deletions without asking (not recommended)")
        box.addWidget(self.allow_large_deletes_check)

        note = QLabel(
            "A source that fails to mount looks like an empty folder. MilBack "
            "will not delete anything if a source scans as empty.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #888;")
        box.addWidget(note)

        group.setLayout(box)
        return group

    def _schedule_group(self):
        group = QGroupBox("Schedule Automation")
        row = QHBoxLayout()
        row.addWidget(QLabel("Frequency:"))
        self.sched_type_combo = QComboBox()
        self.sched_type_combo.addItems(["Manual", "Daily", "Weekly"])
        self.sched_type_combo.currentTextChanged.connect(self.toggle_schedule_ui)
        row.addWidget(self.sched_type_combo)

        self.day_label = QLabel("Day:")
        row.addWidget(self.day_label)
        self.sched_day_combo = QComboBox()
        self.sched_day_combo.addItems(profile_store.DAYS)
        row.addWidget(self.sched_day_combo)

        self.time_label = QLabel("Time:")
        row.addWidget(self.time_label)
        self.sched_time = QTimeEdit()
        self.sched_time.setDisplayFormat("HH:mm")
        row.addWidget(self.sched_time)

        self.catch_up_check = QCheckBox("Run a missed backup at the next opportunity")
        self.catch_up_check.setChecked(True)
        row.addWidget(self.catch_up_check)
        row.addStretch()
        group.setLayout(row)
        self.toggle_schedule_ui("Manual")
        return group

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            event.ignore()
            self.hide()
            self.tray_icon.showMessage(
                "MilBack is still running", "A backup is in progress.",
                QSystemTrayIcon.MessageIcon.Information, 2000)
            return
        self.save_current_profile(quiet=True)
        event.ignore()
        self.hide()
        self.tray_icon.showMessage(
            "MilBack is Active", "Running in the background for scheduled backups.",
            QSystemTrayIcon.MessageIcon.Information, 2000)

    def show_normal(self):
        self.show()
        self.activateWindow()

    def force_quit(self):
        if self.worker and self.worker.isRunning():
            answer = QMessageBox.question(
                self, "Backup in progress", "A backup is running. Stop it and quit?")
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.worker.stop()
            self.worker.wait(10000)
        self.save_current_profile(quiet=True)
        QApplication.quit()

    def toggle_schedule_ui(self, text):
        self.sched_day_combo.setEnabled(text == "Weekly")
        self.day_label.setEnabled(text == "Weekly")
        self.sched_time.setEnabled(text != "Manual")
        self.time_label.setEnabled(text != "Manual")
        self.catch_up_check.setEnabled(text != "Manual")

    def load_all_profiles(self):
        self.profiles = profile_store.load()
        self.profile_list.clear()
        for name in sorted(self.profiles):
            self.profile_list.addItem(name)

    def on_profile_changed(self, current, previous):
        # The outgoing profile is written first, or edits made without pressing
        # Save would vanish the moment the user clicks another profile.
        if previous is not None and not self._loading:
            name = previous.text()
            if name in self.profiles:
                self.profiles[name] = self.collect_settings()
                self._write_profiles()
        if current is not None:
            self.load_profile_data(current.text())

    def load_profile_data(self, name):
        data = self.profiles.get(name)
        if data is None:
            return
        self._loading = True
        try:
            self.current_profile_name = name
            self.settings_pane.setEnabled(True)
            self.profile_title.setText(f"Profile: {name}")

            self.job_table.setRowCount(0)
            for job in data.get("jobs", []):
                row = self.job_table.rowCount()
                self.job_table.insertRow(row)
                self.job_table.setItem(row, 0, QTableWidgetItem(job["src"]))
                self.job_table.setItem(row, 1, QTableWidgetItem(job["dst"]))

            self.backup_mode_combo.setCurrentText(data.get("mode", MODE_INCREMENTAL))
            self.workers_spin.setValue(data.get("workers", 4))
            self.deep_verify_check.setChecked(data.get("deep", False))
            self.quick_verify_check.setChecked(data.get("quick_verify", False))
            self.quick_verify_check.setEnabled(data.get("deep", False))
            self.trust_index_check.setChecked(data.get("trust_index", False))
            self.retry_spin.setValue(data.get("retries", 5))
            self.wait_spin.setValue(data.get("wait", 30))
            self.versioning_combo.setCurrentText(data.get("versioning", VERSION_NONE))
            self.retention_spin.setValue(data.get("retention_days", 30))
            self.delete_ratio_spin.setValue(data.get("max_delete_ratio", 0.20))
            self.allow_large_deletes_check.setChecked(
                data.get("allow_large_deletes", False))
            self.sched_type_combo.setCurrentText(data.get("sched_type", "Manual"))
            self.sched_day_combo.setCurrentText(data.get("sched_day", "Monday"))
            self.sched_time.setTime(
                QTime.fromString(data.get("sched_time", "00:00"), "HH:mm"))
            self.catch_up_check.setChecked(data.get("catch_up", True))
        finally:
            self._loading = False
        self.refresh_last_run()

    def collect_settings(self):
        jobs = []
        for row in range(self.job_table.rowCount()):
            src = self.job_table.item(row, 0)
            dst = self.job_table.item(row, 1)
            if src and dst and src.text() and dst.text():
                jobs.append({"src": src.text(), "dst": dst.text()})
        return {
            "jobs": jobs,
            "mode": self.backup_mode_combo.currentText(),
            "workers": self.workers_spin.value(),
            "deep": self.deep_verify_check.isChecked(),
            "quick_verify": self.quick_verify_check.isChecked(),
            "trust_index": self.trust_index_check.isChecked(),
            "retries": self.retry_spin.value(),
            "wait": self.wait_spin.value(),
            "versioning": self.versioning_combo.currentText(),
            "retention_days": self.retention_spin.value(),
            "max_delete_ratio": self.delete_ratio_spin.value(),
            "allow_large_deletes": self.allow_large_deletes_check.isChecked(),
            "sched_type": self.sched_type_combo.currentText(),
            "sched_day": self.sched_day_combo.currentText(),
            "sched_time": self.sched_time.time().toString("HH:mm"),
            "catch_up": self.catch_up_check.isChecked(),
        }

    def save_current_profile(self, quiet=False):
        if not self.current_profile_name:
            return
        self.profiles[self.current_profile_name] = self.collect_settings()
        if self._write_profiles() and not quiet:
            self.log.append(f"Profile '{self.current_profile_name}' saved.")

    def _write_profiles(self):
        try:
            profile_store.save(self.profiles)
            return True
        except OSError as e:
            QMessageBox.critical(self, "Could not save profiles", str(e))
            return False

    def new_profile(self):
        name, ok = QInputDialog.getText(self, "New Profile", "Enter Profile Name:")
        name = name.strip() if ok else ""
        if not name:
            return
        if name in self.profiles:
            QMessageBox.warning(self, "Name in use", f"'{name}' already exists.")
            return
        self.profiles[name] = profile_store.new_profile()
        self._write_profiles()
        self.profile_list.addItem(name)
        self.profile_list.setCurrentRow(self.profile_list.count() - 1)

    def delete_profile(self):
        item = self.profile_list.currentItem()
        if not item:
            return
        name = item.text()
        if QMessageBox.question(
                self, "Delete profile",
                f"Delete '{name}'? This does not touch any backed up files."
        ) != QMessageBox.StandardButton.Yes:
            return
        self.profiles.pop(name, None)
        self.current_profile_name = None
        self._loading = True
        self.profile_list.takeItem(self.profile_list.row(item))
        self._loading = False
        self._write_profiles()
        self.settings_pane.setEnabled(False)
        self.profile_title.setText("Select a Profile to begin")
        self.job_table.setRowCount(0)

    def add_job(self):
        src = QFileDialog.getExistingDirectory(self, "Select Source")
        if not src:
            return
        dst = QFileDialog.getExistingDirectory(self, "Select Destination")
        if not dst:
            return
        if os.path.abspath(dst).startswith(os.path.abspath(src) + os.sep):
            QMessageBox.warning(
                self, "Destination inside source",
                "The destination is inside the source folder. MilBack will skip "
                "it during the scan, but a separate folder is a better choice.")
        row = self.job_table.rowCount()
        self.job_table.insertRow(row)
        self.job_table.setItem(row, 0, QTableWidgetItem(src))
        self.job_table.setItem(row, 1, QTableWidgetItem(dst))

    def remove_job(self):
        row = self.job_table.currentRow()
        if row >= 0:
            self.job_table.removeRow(row)

    def refresh_last_run(self):
        status = load_status().get(self.current_profile_name)
        if not status:
            self.last_run_label.setText("No recorded run yet.")
            return
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(status.get("finished", 0)))
        bits = [f"Last run {when}",
                f"{status.get('files_copied', 0):,} copied",
                human(status.get("bytes_copied", 0))]
        if status.get("files_failed"):
            bits.append(f"{status['files_failed']:,} failed")
        if status.get("scan_errors"):
            bits.append(f"{status['scan_errors']:,} scan errors")
        if status.get("dry_run"):
            bits.append("(dry run)")
        self.last_run_label.setText("  |  ".join(bits))

    def start_backup(self, dry_run=False, profile_name=None):
        if self.worker and self.worker.isRunning():
            return
        scheduled = profile_name is not None
        name = profile_name or self.current_profile_name
        if not name:
            return
        if not scheduled:
            self.save_current_profile(quiet=True)
        data = self.profiles.get(name)
        if not data:
            return
        if not data.get("jobs"):
            if not scheduled:
                QMessageBox.warning(self, "Nothing to do",
                                    "This profile has no backup jobs.")
            return
        if data.get("mode") == MODE_MIRROR and not dry_run and not scheduled:
            if QMessageBox.question(
                    self, "Mirror mode",
                    "Mirror mode deletes files from the destination that are no "
                    "longer in the source. Run a dry run first if you are unsure."
                    "\n\nContinue?") != QMessageBox.StandardButton.Yes:
                return

        settings = profile_store.to_settings(data)
        settings["dry_run"] = dry_run

        self.total_bytes = 0.0
        self.bytes_copied = 0
        self.start_time = time.time()
        self.progress_bar.setRange(0, 0)

        self.worker = BackupWorker(settings, name)
        self.worker.progress_update.connect(self.append_log)
        self.worker.error_found.connect(self.append_log)
        self.worker.task_stats_ready.connect(self.update_totals)
        self.worker.chunk_finished.connect(self.update_live_stats)
        self.worker.finished.connect(self.on_complete)
        self.start_btn.setEnabled(False)
        self.dry_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.worker.start()

    def stop_backup(self):
        if self.worker:
            self.append_log("Stopping after the current file...")
            self.worker.stop()

    def append_log(self, line):
        self.log.append(line)

    def update_totals(self, files, total_bytes):
        self.total_bytes = float(total_bytes) or 1.0
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 100)

    def update_live_stats(self, bytes_done):
        # The engine sends a running total, not a delta, so a dropped update
        # cannot leave the bar permanently out of step.
        self.bytes_copied = int(bytes_done)
        if self.total_bytes > 0:
            percent = min(100, max(0, int(self.bytes_copied / self.total_bytes * 100)))
            self.progress_bar.setValue(percent)
            elapsed = max(time.time() - self.start_time, 0.001)
            self.stats_label.setText(
                f"{human(self.bytes_copied)} of about {human(self.total_bytes)}  |  "
                f"{human(self.bytes_copied / elapsed)}/s  |  {percent}%")

    def on_complete(self, found, copied, in_use):
        self.start_btn.setEnabled(True)
        self.dry_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.refresh_last_run()
        if in_use:
            self.append_log(f"{in_use:,} file(s) were in use and were skipped.")

    def check_schedule(self):
        if self.worker and self.worker.isRunning():
            return
        status = load_status()
        for name, data in self.profiles.items():
            record = status.get(name) or {}
            last = 0 if record.get("dry_run") else record.get("finished", 0)
            if profile_store.is_due(data, last):
                self.append_log(f"Scheduled run starting: '{name}'")
                self.start_backup(dry_run=False, profile_name=name)
                return


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = MilBackWindow()
    if "--tray" not in sys.argv:
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
