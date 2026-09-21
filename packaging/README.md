# Running MilBack without the GUI

The GUI only runs scheduled backups while its window or tray icon is alive.
For unattended machines, use the systemd timer instead.

## systemd user timer (recommended)

    mkdir -p ~/.config/systemd/user
    cp milback-backup.service milback-backup.timer ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now milback-backup.timer

The timer wakes every 15 minutes and runs any profile whose scheduled time has
passed since its last successful run. `Persistent=true` means a backup that was
due while the machine was off runs once it comes back up.

Check on it with:

    systemctl --user list-timers milback-backup.timer
    journalctl --user -u milback-backup.service -n 50

If the backups must run when you are not logged in:

    loginctl enable-linger $USER

## Autostart the tray instead

If you would rather have the GUI handle scheduling, copy the desktop entry:

    cp milback-tray.desktop ~/.config/autostart/

Use one or the other, not both.

## Command line

    python3 cli.py --list                 # profiles and their last run
    python3 cli.py "My Profile"           # run one profile now
    python3 cli.py "My Profile" --dry-run # show what would happen
    python3 cli.py --all                  # run everything
    python3 cli.py                        # run whatever is due

Exit status is non-zero if any file failed to copy or the scan reported errors,
so the timer surfaces failures in `systemctl --user status`.
