# MilBack

**MilBack** is a resilient file and network backup engine for Linux, built for
copying to network shares that are slow, flaky, or both.

### Features

* **Built for slow shares.** Parallel transfers hide network latency, copying
  starts on the first file found rather than after the whole tree is scanned,
  and a local index lets repeat runs skip unchanged files without asking the
  destination about them.
* **Resilient.** Block-level read retries, retries on directory listings, and
  interrupted transfers resume where they stopped instead of starting over.
* **Careful with deletions.** Mirror mode refuses to delete when a source
  scans as empty, when the scan reported errors, or when the deletions would
  exceed a configurable share of the backup.
* **Versioning.** Optionally keep replaced and removed files for a set number
  of days, as copies or as hardlinks.
* **Multi-profile management** with scheduling that catches up on runs missed
  while the machine was off.
* **Runs without the GUI** via a command line tool and a systemd timer.
* **Simple GUI** built with Python and PyQt6.

### Installing

Download the latest `.deb` from the [Releases](https://github.com/thinusmilner1979-oss/MilBack/releases)
page and install it with your package manager, then launch MilBack from the
application menu.

To run from source:

    pip install PyQt6
    python3 main.py

### Unattended backups

The GUI only runs scheduled backups while it is open. For a machine that should
back up on its own, install the systemd timer — see [packaging/README.md](packaging/README.md).

    python3 cli.py --list                  # profiles and their last run
    python3 cli.py "My Profile" --dry-run  # show what would happen
    python3 cli.py "My Profile"            # run it
    python3 cli.py                         # run whatever is due

### Diagnosing a slow backup

    python3 milback_doctor.py

Measures metadata round-trip latency, directory listing cost and write speed
against your actual paths, and suggests how many parallel transfers to use.
On a high-latency share, metadata round trips usually dominate; turning on
**Fast incremental** and raising **Parallel transfers** is what helps.

### Where things live

| What | Where |
| --- | --- |
| Profiles | `~/.config/milback/backup_profiles.json` |
| File index | `~/.local/share/milback/index.sqlite3` |
| Run logs | `~/.local/share/milback/logs/` |
| Kept versions | `.milback-versions/` beside the backed up files |

### What it does not do

MilBack copies file contents, timestamps and permission bits. It is not a
system imaging tool: it does not preserve ownership, extended attributes,
hardlink structure, sparseness, or device nodes, and symlinked directories are
followed rather than recreated as links.

### Tests

    python3 tests/test_engine.py
    python3 tests/test_schedule.py

### Tech stack

* **Language:** Python 3
* **GUI Framework:** PyQt6
* **Platform:** Linux (Debian-based)
