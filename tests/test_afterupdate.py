"""What broke after an update: package logs of pacman / apt / rpm, sessions, suspects, reboot notes."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shellmate import afterupdate as A, pkglog, system  # noqa: E402
from shellmate.pkglog import Change, Txn  # noqa: E402

PACMAN_LOG = """[2026-10-09T19:37:02+0300] [PACMAN] starting full system upgrade
[2026-10-09T19:40:00+0300] [ALPM] transaction started
[2026-10-09T19:40:01+0300] [ALPM] upgraded linux (7.2.6.arch2-1 -> 7.2.9.arch1-1)
[2026-10-09T19:40:02+0300] [ALPM] upgraded pipewire (1:1.4.8-1 -> 1:1.4.9-1)
[2026-10-09T19:40:02+0300] [ALPM] installed python-new (1.0-1)
[2026-10-09T19:40:03+0300] [ALPM] removed old-thing (2.0-1)
[2026-10-09T19:40:04+0300] [ALPM] transaction completed
[2026-10-09T20:31:00+0300] [ALPM] transaction started
[2026-10-09T20:31:01+0300] [ALPM] upgraded some-aur-git (r1-1 -> r2-1)
[2026-10-09T20:31:02+0300] [ALPM] transaction completed
[2026-10-20T12:57:57+0300] [ALPM] transaction started
[2026-10-20T12:57:57+0300] [ALPM] installed valkey (9.1.2-1.1)
[2026-10-20T12:57:58+0300] [ALPM] transaction completed
"""

APT_LOG = """Start-Date: 2026-05-01  10:00:01
Commandline: apt upgrade
Upgrade: libc6:amd64 (2.35-0ubuntu3.6, 2.35-0ubuntu3.7), pipewire:amd64 (1.0.5-1, 1.0.6-1)
Install: linux-image-6.8.0-50-generic:amd64 (6.8.0-50.51, automatic)
End-Date: 2026-05-01  10:00:30

Start-Date: 2026-05-02  09:00:00
Commandline: apt remove foo
Remove: foo:amd64 (2.0-1)
End-Date: 2026-05-02  09:00:03
"""


class Logs(unittest.TestCase):
    def test_pacman(self):
        txns = pkglog.parse_pacman_log(PACMAN_LOG)
        self.assertEqual(len(txns), 3)
        self.assertEqual(txns[0].changes[0], Change("linux", "7.2.6.arch2-1", "7.2.9.arch1-1", "upgraded"))
        self.assertEqual(txns[0].changes[2], Change("python-new", "", "1.0-1", "installed"))
        self.assertEqual(txns[0].changes[3], Change("old-thing", "2.0-1", "", "removed"))
        self.assertEqual([c.name for c in txns[0].upgrades], ["linux", "pipewire"])

    def test_apt(self):
        txns = pkglog.parse_apt_history(APT_LOG)
        self.assertEqual(txns[0].changes[:2], [Change("libc6", "2.35-0ubuntu3.6", "2.35-0ubuntu3.7", "upgraded"),
                                               Change("pipewire", "1.0.5-1", "1.0.6-1", "upgraded")])
        self.assertEqual(txns[0].changes[2], Change("linux-image-6.8.0-50-generic", "", "6.8.0-50.51", "installed"))
        self.assertEqual(txns[1].changes, [Change("foo", "2.0-1", "", "removed")])

    def test_rpm(self):
        txns = pkglog.parse_rpm_installtimes("1700000000 kernel 6.8.5-301.fc40\n1700000100 mesa 24.0-1.fc40\n"
                                             "1700900000 htop 3.3-1.fc40\n(none)\n")
        self.assertEqual([[c.name for c in tx.changes] for tx in txns], [["kernel", "mesa"], ["htop"]])


class Analysis(unittest.TestCase):
    def test_sessions_and_last_update(self):
        txns = pkglog.parse_pacman_log(PACMAN_LOG)
        sess = A.sessions(txns)
        self.assertEqual(len(sess), 2)  # the AUR run 51 minutes later is the same update
        upd = A.last_update(txns)  # the last session with upgrades, not the fresh install
        self.assertEqual({c.name for c in upd.changes}, {"linux", "pipewire", "python-new", "old-thing", "some-aur-git"})
        self.assertEqual(A.version_arrow(next(c for c in upd.changes if c.name == "pipewire")), "pipewire 1.4.8-1 → 1.4.9-1")
        self.assertEqual(A.important(upd.upgrades)[0].name, "linux")

    def test_subsystem(self):
        self.assertEqual(A.subsystem_of("после обновления пропал звук"), "sound")
        self.assertEqual(A.subsystem_of("wifi stopped working after the update"), "network")
        self.assertEqual(A.subsystem_of("что сломалось после обновления"), "")

    def test_new_errors_and_suspects(self):
        old = [system.LogError("kernel", "usb 1-2: device descriptor read/64, error -71", 3)]
        new = [system.LogError("kernel", "usb 1-3: device descriptor read/64, error -71", 1),
               system.LogError("wireplumber", "spa.alsa: hw:0: snd_pcm_open failed", 2),
               system.LogError("kernel", "NVRM: GPU at PCI:0000:01:00: GPU has fallen off the bus", 1)]
        fresh = A.new_errors(old, new)
        self.assertEqual([e.source for e in fresh], ["wireplumber", "kernel"])  # numbers don't make an error new
        changes = [Change("linux", "1", "2", "upgraded"), Change("pipewire", "1", "2", "upgraded"),
                   Change("nvidia-utils", "580", "585", "upgraded"), Change("linux-headers", "1", "2", "upgraded")]
        found = A.suspects(fresh, changes, owner=lambda path: None)
        self.assertEqual(found[0].changes[0].name, "pipewire")
        self.assertEqual(found[1].changes[0].name, "nvidia-utils")
        self.assertNotIn("linux-headers", [c.name for s in found for c in s.changes])
        strong = A.suspects([system.LogError("ls", "boom", 1)], [Change("coreutils", "1", "2", "upgraded")],
                            owner=lambda path: "coreutils")
        self.assertTrue(strong[0].strong)

    def test_reboot_notes(self):
        now = datetime.now().astimezone()
        upd = Txn(now - timedelta(hours=2), [Change("linux", "7.2.6-1", "7.2.9-1", "upgraded")])
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "7.2.9-1").mkdir()
            gone = A.reboot_notes(upd, [(now - timedelta(days=1), now)], running="7.2.6-1", modules=Path(d))
            self.assertEqual(len(gone), 1)  # the running kernel's modules are gone
            (Path(d) / "7.2.6-1").mkdir()
            stale = A.reboot_notes(upd, [(now - timedelta(days=1), now)], running="7.2.6-1", modules=Path(d))
            self.assertEqual(len(stale), 1)  # not rebooted since a kernel update
            fine = A.reboot_notes(upd, [(now - timedelta(hours=1), now)], running="7.2.9-1", modules=Path(d))
            self.assertEqual(fine, [])


if __name__ == "__main__":
    unittest.main()
