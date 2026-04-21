import sys
import os
import json
import time
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QListWidget, QFileDialog, 
                             QLabel, QProgressBar, QTextEdit, QCheckBox, QSpinBox, 
                             QGroupBox, QTableWidget, QTableWidgetItem, QComboBox, 
                             QInputDialog, QMessageBox, QHeaderView, QTimeEdit,
                             QSystemTrayIcon, QMenu, QStyle)
from PyQt6.QtCore import Qt, QTimer, QTime, QDate
from PyQt6.QtGui import QAction
from engine import BackupWorker

class CobianLinux(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MilBack v1.0.0")
        self.resize(1100, 850)
        
        # --- LINUX PACKAGING FIX: Save to User's Home Directory ---
        config_dir = os.path.expanduser("~/.config/milback")
        os.makedirs(config_dir, exist_ok=True)
        self.config_file = os.path.join(config_dir, "backup_profiles.json")
        
        self.profiles = {}
        self.current_profile_name = None
        self.total_bytes = 0
        self.bytes_copied = 0

        # --- SYSTEM TRAY SETUP ---
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DriveNetIcon))
        
        tray_menu = QMenu()
        show_action = QAction("Show Dashboard", self)
        show_action.triggered.connect(self.show_normal)
        quit_action = QAction("Exit MilBack", self)
        quit_action.triggered.connect(self.force_quit)
        
        tray_menu.addAction(show_action)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

        # --- BACKGROUND SCHEDULER ---
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_schedule)
        self.timer.start(60000)
        self.last_run_profile = None
        self.last_run_time = None

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        self.main_layout = QHBoxLayout(main_widget)

        # --- SIDEBAR ---
        sidebar = QVBoxLayout()
        sidebar.addWidget(QLabel("<b>Profiles:</b>"))
        self.profile_list = QListWidget()
        self.profile_list.itemClicked.connect(self.load_profile_data)
        sidebar.addWidget(self.profile_list)

        side_btns = QHBoxLayout()
        self.add_prof_btn = QPushButton("New")
        self.add_prof_btn.clicked.connect(self.new_profile)
        self.del_prof_btn = QPushButton("Delete")
        self.del_prof_btn.clicked.connect(self.delete_profile)
        side_btns.addWidget(self.add_prof_btn)
        side_btns.addWidget(self.del_prof_btn)
        sidebar.addLayout(side_btns)
        self.main_layout.addLayout(sidebar, 1)

        # --- MAIN PANE ---
        self.settings_pane = QWidget()
        self.settings_pane.setEnabled(False) 
        pane_layout = QVBoxLayout(self.settings_pane)

        self.profile_title = QLabel("Select a Profile to begin")
        self.profile_title.setStyleSheet("font-size: 16px; font-weight: bold; color: #2e7d32;")
        pane_layout.addWidget(self.profile_title)

        pane_layout.addWidget(QLabel("<b>Backup Jobs (Source -> Destination):</b>"))
        self.job_table = QTableWidget(0, 2)
        self.job_table.setHorizontalHeaderLabels(["Source Folder", "Destination Folder"])
        self.job_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        pane_layout.addWidget(self.job_table)

        job_btns = QHBoxLayout()
        self.add_job_btn = QPushButton("Add Job")
        self.add_job_btn.clicked.connect(self.add_job)
        self.rem_job_btn = QPushButton("Remove Selected Job")
        self.rem_job_btn.clicked.connect(self.remove_job)
        job_btns.addWidget(self.add_job_btn)
        job_btns.addWidget(self.rem_job_btn)
        pane_layout.addLayout(job_btns)

        options_group = QGroupBox("Backup Options")
        options_layout = QVBoxLayout()
        
        type_layout = QHBoxLayout()
        type_layout.addWidget(QLabel("Backup Type:"))
        self.backup_mode_combo = QComboBox()
        self.backup_mode_combo.addItems([
            "Add & Update (Incremental)", 
            "Exact Sync (Mirror)", 
            "Full Overwrite"
        ])
        type_layout.addWidget(self.backup_mode_combo)
        type_layout.addStretch()
        options_layout.addLayout(type_layout)

        self.deep_verify_check = QCheckBox("Deep Verify: MD5 hash comparison (Slower/Safer)")
        options_layout.addWidget(self.deep_verify_check)
        options_group.setLayout(options_layout)
        pane_layout.addWidget(options_group)

        res_layout = QHBoxLayout()
        res_layout.addWidget(QLabel("Block Retries:"))
        self.retry_spin = QSpinBox()
        self.retry_spin.setValue(5)
        res_layout.addWidget(self.retry_spin)
        res_layout.addWidget(QLabel("Wait Window (mins):"))
        self.wait_spin = QSpinBox()
        self.wait_spin.setValue(30)
        res_layout.addWidget(self.wait_spin)
        pane_layout.addLayout(res_layout)

        # --- SCHEDULER UI ---
        sched_group = QGroupBox("Schedule Automation")
        sched_layout = QHBoxLayout()
        
        sched_layout.addWidget(QLabel("Frequency:"))
        self.sched_type_combo = QComboBox()
        self.sched_type_combo.addItems(["Manual", "Daily", "Weekly"])
        self.sched_type_combo.currentTextChanged.connect(self.toggle_schedule_ui)
        sched_layout.addWidget(self.sched_type_combo)

        self.day_label = QLabel("Day:")
        sched_layout.addWidget(self.day_label)
        self.sched_day_combo = QComboBox()
        self.sched_day_combo.addItems(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])
        sched_layout.addWidget(self.sched_day_combo)

        self.time_label = QLabel("Time:")
        sched_layout.addWidget(self.time_label)
        self.sched_time = QTimeEdit()
        self.sched_time.setDisplayFormat("HH:mm")
        sched_layout.addWidget(self.sched_time)
        
        sched_layout.addStretch()
        sched_group.setLayout(sched_layout)
        pane_layout.addWidget(sched_group)
        self.toggle_schedule_ui("Manual")

        # SAVE BUTTON
        self.save_btn = QPushButton("SAVE PROFILE SETTINGS")
        self.save_btn.setFixedHeight(40)
        self.save_btn.setStyleSheet("background-color: #0d47a1; color: white; font-weight: bold;")
        self.save_btn.clicked.connect(self.save_all_profiles)
        pane_layout.addWidget(self.save_btn)

        exec_layout = QHBoxLayout()
        self.start_btn = QPushButton("START PROFILE BACKUP")
        self.start_btn.setFixedHeight(50)
        self.start_btn.setStyleSheet("background-color: #1b5e20; color: white; font-weight: bold;")
        self.start_btn.clicked.connect(lambda: self.start_backup(self.current_profile_name, self.get_current_settings()))
        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setFixedHeight(50)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setStyleSheet("background-color: #b71c1c; color: white; font-weight: bold;")
        self.stop_btn.clicked.connect(self.stop_backup)
        exec_layout.addWidget(self.start_btn, 2)
        exec_layout.addWidget(self.stop_btn, 1)
        pane_layout.addLayout(exec_layout)

        self.stats_label = QLabel("Ready.")
        pane_layout.addWidget(self.stats_label)
        self.progress_bar = QProgressBar()
        pane_layout.addWidget(self.progress_bar)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("background: #000; color: #33ff33; font-family: monospace;")
        pane_layout.addWidget(self.log)

        self.main_layout.addWidget(self.settings_pane, 3)
        self.load_all_profiles()

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self.tray_icon.showMessage("MilBack is Active", "Running in the background for scheduled backups.", QSystemTrayIcon.MessageIcon.Information, 2000)

    def show_normal(self):
        self.show()
        self.activateWindow()

    def force_quit(self):
        self.stop_backup()
        QApplication.quit()

    def toggle_schedule_ui(self, text):
        self.sched_day_combo.setEnabled(text == "Weekly")
        self.sched_time.setEnabled(text != "Manual")
        self.day_label.setEnabled(text == "Weekly")
        self.time_label.setEnabled(text != "Manual")

    def check_schedule(self):
        if hasattr(self, 'worker') and self.worker and self.worker.isRunning(): return
        current_time = QTime.currentTime().toString("HH:mm")
        current_day = QDate.currentDate().toString("dddd") 

        for profile_name, data in self.profiles.items():
            sched_type = data.get('sched_type', 'Manual')
            if sched_type == 'Manual': continue
            
            sched_time = data.get('sched_time', '00:00')
            sched_day = data.get('sched_day', 'Monday')
            should_run = False
            if sched_type == 'Daily' and current_time == sched_time: should_run = True
            elif sched_type == 'Weekly' and current_time == sched_time and current_day == sched_day: should_run = True

            if should_run:
                if self.last_run_profile == profile_name and self.last_run_time == current_time: continue
                self.last_run_profile = profile_name
                self.last_run_time = current_time
                self.log.append(f"\n[{current_time}] AUTOMATIC TRIGGER: Starting scheduled profile '{profile_name}'")
                self.start_backup(profile_name, data)
                break 

    def new_profile(self):
        name, ok = QInputDialog.getText(self, "New Profile", "Enter Profile Name:")
        if ok and name:
            self.profiles[name] = {
                'jobs': [], 'mode': 'Add & Update (Incremental)', 'deep': False, 
                'retries': 5, 'wait': 30, 'sched_type': 'Manual', 
                'sched_day': 'Monday', 'sched_time': '00:00'
            }
            self.profile_list.addItem(name)
            self.save_all_profiles()

    def delete_profile(self):
        current = self.profile_list.currentItem()
        if current:
            name = current.text()
            if name in self.profiles: del self.profiles[name]
            self.current_profile_name = None
            self.profile_list.takeItem(self.profile_list.row(current))
            self.save_all_profiles()
            self.settings_pane.setEnabled(False)
            self.profile_title.setText("Select a Profile to begin")
            self.job_table.setRowCount(0)

    def save_all_profiles(self):
        if self.current_profile_name:
            self.profiles[self.current_profile_name] = self.get_current_settings()
            with open(self.config_file, 'w') as f: json.dump(self.profiles, f)
            self.log.append(f"Profile '{self.current_profile_name}' saved.")

    def load_all_profiles(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    self.profiles = json.load(f)
                    for name in self.profiles: self.profile_list.addItem(name)
            except: pass

    def load_profile_data(self):
        current_item = self.profile_list.currentItem()
        if not current_item: return
        self.current_profile_name = current_item.text()
        data = self.profiles[self.current_profile_name]
        self.settings_pane.setEnabled(True)
        self.profile_title.setText(f"Profile: {self.current_profile_name}")
        self.job_table.setRowCount(0)
        for job in data.get('jobs', []):
            row = self.job_table.rowCount()
            self.job_table.insertRow(row)
            self.job_table.setItem(row, 0, QTableWidgetItem(job['src']))
            self.job_table.setItem(row, 1, QTableWidgetItem(job['dst']))
        
        self.backup_mode_combo.setCurrentText(data.get('mode', 'Add & Update (Incremental)'))
        self.deep_verify_check.setChecked(data.get('deep', False))
        self.retry_spin.setValue(data.get('retries', 5))
        self.wait_spin.setValue(data.get('wait', 30))
        self.sched_type_combo.setCurrentText(data.get('sched_type', 'Manual'))
        self.sched_day_combo.setCurrentText(data.get('sched_day', 'Monday'))
        self.sched_time.setTime(QTime.fromString(data.get('sched_time', '00:00'), "HH:mm"))

    def get_current_settings(self):
        jobs = []
        for i in range(self.job_table.rowCount()):
            jobs.append({'src': self.job_table.item(i, 0).text(), 'dst': self.job_table.item(i, 1).text()})
        return {
            'jobs': jobs, 'mode': self.backup_mode_combo.currentText(),
            'deep': self.deep_verify_check.isChecked(), 'retries': self.retry_spin.value(), 'wait': self.wait_spin.value(),
            'sched_type': self.sched_type_combo.currentText(), 'sched_day': self.sched_day_combo.currentText(),
            'sched_time': self.sched_time.time().toString("HH:mm")
        }

    def add_job(self):
        src = QFileDialog.getExistingDirectory(self, "Select Source")
        if not src: return
        dst = QFileDialog.getExistingDirectory(self, "Select Destination")
        if not dst: return
        row = self.job_table.rowCount()
        self.job_table.insertRow(row)
        self.job_table.setItem(row, 0, QTableWidgetItem(src))
        self.job_table.setItem(row, 1, QTableWidgetItem(dst))

    def remove_job(self):
        self.job_table.removeRow(self.job_table.currentRow())

    def start_backup(self, profile_name, data):
        settings = data.copy()
        settings['wait_timeout'] = settings.get('wait', 30) * 60
        settings['backup_mode'] = settings.get('mode', 'Add & Update (Incremental)')
        settings['deep_verify'] = settings.get('deep', False)
        
        self.total_bytes = 0
        self.bytes_copied = 0
        self.progress_bar.setRange(0, 0)
        
        self.worker = BackupWorker(settings)
        self.worker.progress_update.connect(self.log.append)
        self.worker.error_found.connect(self.log.append)
        self.worker.task_stats_ready.connect(self.setup_progress_bar)
        self.worker.chunk_finished.connect(self.update_live_stats)
        self.worker.finished.connect(self.on_complete)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.start_time = time.time()
        self.worker.start()

    def stop_backup(self):
        if hasattr(self, 'worker') and self.worker: self.worker.stop()

    def setup_progress_bar(self, files, bytes_total):
        self.total_bytes = float(bytes_total) if bytes_total > 0 else 1.0
        self.bytes_copied = 0
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.log.append(f"Scan complete: {files} files found.")

    def update_live_stats(self, chunk_size):
        self.bytes_copied += chunk_size
        if self.total_bytes > 0:
            percent = (self.bytes_copied / self.total_bytes) * 100
            clamped_percent = min(100, max(0, int(percent)))
            self.progress_bar.setValue(clamped_percent)
            
            elapsed = time.time() - self.start_time
            if elapsed > 0:
                speed = self.bytes_copied / elapsed / 1024 / 1024
                self.stats_label.setText(f"Speed: {speed:.2f} MB/s | Progress: {clamped_percent}%")

    def on_complete(self, total, copied, in_use):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.log.append(f"\n--- PROFILE FINISHED ---")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    w = CobianLinux()
    w.show()
    sys.exit(app.exec())
