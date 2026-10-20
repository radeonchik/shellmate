"""Parsers of package-manager output, with samples of the real formats (dnf4 and dnf5 differ).
The live counterpart is `shellmate --selftest`, run in real distro containers by CI."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shellmate import backends, packages  # noqa: E402
from shellmate.backends import apt, dnf, pacman  # noqa: E402


class OsRelease(unittest.TestCase):
    def test_families(self):
        cases = {
            'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nNAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\n': ("debian", "Ubuntu 24.04.1 LTS"),
            'PRETTY_NAME="Linux Mint 22"\nID=linuxmint\nID_LIKE="ubuntu debian"\n': ("debian", "Linux Mint 22"),
            'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\n': ("debian", "Debian GNU/Linux 12 (bookworm)"),
            'NAME="Fedora Linux"\nPRETTY_NAME="Fedora Linux 41 (Workstation Edition)"\nID=fedora\n': ("fedora", "Fedora Linux 41 (Workstation Edition)"),
            'PRETTY_NAME="CachyOS"\nID=cachyos\nID_LIKE=arch\n': ("arch", "CachyOS"),
            'PRETTY_NAME="Rocky Linux 9.4"\nID="rocky"\nID_LIKE="rhel centos fedora"\n': ("fedora", "Rocky Linux 9.4"),
        }
        for text, (family, name) in cases.items():
            d = backends.parse_os_release(text)
            self.assertEqual((d.family, d.name), (family, name), text)


class Pacman(unittest.TestCase):
    def test_search(self):
        out = ("extra/obs-studio 32.2.2-1 [6.69 MiB 24.71 MiB] [installed]\n"
               "    Free, open source software for live streaming and recording\n"
               "aur/obs-studio-git 32.2.2.r4.g1bf1379-1 [+112 ~0.02]\n"
               "    Free, open source software for live streaming and recording\n")
        pkgs = pacman.parse_pacman(out)
        self.assertEqual([(p.source, p.name, p.installed, p.votes) for p in pkgs],
                         [("extra", "obs-studio", True, 0), ("aur", "obs-studio-git", False, 112)])


class Apt(unittest.TestCase):
    def test_search(self):
        out = ("obs-studio - recorder and streamer for live video content\n"
               "obs-plugins - recorder and streamer for live video content (plugins)\n"
               "garbage line\n")
        pkgs = apt.parse_search(out, {"obs-studio"})
        self.assertEqual([(p.name, p.installed) for p in pkgs], [("obs-studio", True), ("obs-plugins", False)])

    def test_dpkg_list(self):
        out = ("bash\t5.2.15-2+b7\tii \tGNU Bourne Again SHell\n"
               "libfoo:amd64\t1.0-1\trc \tremoved, config left\n")
        self.assertEqual([(p.name, p.version) for p in apt.parse_dpkg_list(out)], [("bash", "5.2.15-2+b7")])

    def test_madison(self):
        out = (" firefox-esr | 128.5.0esr-1~deb12u1 | http://deb.debian.org/debian-security bookworm-security/main amd64 Packages\n"
               " firefox-esr | 115.12.0esr-1~deb12u1 | http://deb.debian.org/debian bookworm/main amd64 Packages\n")
        self.assertEqual(apt.parse_madison(out), ["128.5.0esr-1~deb12u1", "115.12.0esr-1~deb12u1"])

    def test_history(self):
        text = ("Start-Date: 2026-09-01  10:00:01\nCommandline: apt upgrade\n"
                "Upgrade: libc6:amd64 (2.36-9, 2.36-9+deb12u4)\nEnd-Date: 2026-09-01  10:01:00\n\n"
                "Start-Date: 2026-09-05  09:00:00\nCommandline: apt install htop\nInstall: htop:amd64 (3.2.2-2)\n"
                "End-Date: 2026-09-05  09:00:10\n")
        self.assertEqual(apt.parse_history(text).strftime("%Y-%m-%d %H:%M"), "2026-09-01 10:00")


class Dnf(unittest.TestCase):
    def test_repoquery(self):
        out = ("obs-studio\t30.2.3-1.fc41\tOpen Broadcaster Software Studio\n"
               "obs-studio\t30.2.3-1.fc41\tOpen Broadcaster Software Studio\n"
               "obs-studio-devel\t30.2.3-1.fc41\tOBS Studio -- Development files\n")
        self.assertEqual([p.name for p in dnf.parse_qf(out, set())], ["obs-studio", "obs-studio-devel"])

    def test_search_dnf4_and_dnf5(self):
        dnf4 = ("========== Name Exactly Matched: obs-studio ==========\n"
                "obs-studio.x86_64 : Open Broadcaster Software Studio\n"
                "obs-studio-devel.x86_64 : Open Broadcaster Software Studio -- Development files\n")
        dnf5 = ("Matched fields: name (exact)\n obs-studio.x86_64\tOpen Broadcaster Software Studio\n"
                "Matched fields: name\n obs-studio-devel.x86_64\tOpen Broadcaster Software Studio -- Development files\n")
        for out in (dnf4, dnf5):
            pkgs = dnf.parse_search(out, set())
            self.assertEqual([p.name for p in pkgs], ["obs-studio", "obs-studio-devel"], out)
            self.assertEqual(pkgs[0].desc, "Open Broadcaster Software Studio")

    def test_provides(self):
        out = ("coreutils-9.5-1.fc41.x86_64 : A set of basic GNU tools commonly used in shell scripts\n"
               "Repo        : @System\nMatched from:\nFilename    : /usr/bin/ls\n\n"
               "coreutils-single-9.5-1.fc41.x86_64 : coreutils multicall binary\n")
        self.assertEqual(dnf.parse_provides(out), ["coreutils", "coreutils-single"])

    def test_list_versions(self):
        out = ("Installed Packages\nfirefox.x86_64   131.0-1.fc41   @updates\n"
               "Available Packages\nfirefox.x86_64   130.0.1-1.fc41   fedora\nfirefox.x86_64   131.0-1.fc41   updates\n")
        self.assertEqual(dnf.parse_list_versions(out, "firefox"), ["131.0-1.fc41", "130.0.1-1.fc41"])


class Snap(unittest.TestCase):
    def test_find_and_list(self):
        find = ("Name     Version   Publisher   Notes    Summary\n"
                "code     abc123    vscode✓     classic  Code editing. Redefined.\n"
                "codium   1.95      snapcrafters  -      Free/Libre Open Source Software Binaries of VSCode\n")
        pkgs = packages.parse_snap(find)
        self.assertEqual([(p.name, p.source) for p in pkgs], [("code", "snap"), ("codium", "snap")])
        self.assertEqual(pkgs[0].desc, "Code editing. Redefined.")
        self.assertEqual(packages.parse_snap("No matching snaps for \"zzz\"\n"), [])


if __name__ == "__main__":
    unittest.main()
