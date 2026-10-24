"""What a shell command does, before anyone runs it: parsing and risk rules, all in code.

Nothing here executes the command or any part of it: no eval, no `bash -n`, no `--help`. The command
is split into simple commands (pipes, &&, ;, redirects, $( ) and fish ( ) substitutions, sh -c '…'),
wrapper words (sudo, env, xargs, nohup…) are taken off, and table-driven rules look for what
people paste from forums without reading: a downloaded script piped into a shell, rm -rf on the
home folder, dd onto a disk, a third-party repository, a partial upgrade on Arch."""

import re
import shlex
from dataclasses import dataclass, field

from shellmate.i18n import t

SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "ash"}
INTERPRETERS = SHELLS | {"python", "python3", "perl", "ruby", "node", "php", "lua"}
DOWNLOADERS = {"curl", "wget", "fetch", "aria2c", "http", "xh"}
# words that run the rest of the line as another command
WRAPPERS = {"sudo", "doas", "pkexec", "run0", "env", "nohup", "nice", "ionice", "time", "exec", "command",
            "builtin", "timeout", "stdbuf", "xargs", "chrt", "taskset", "unbuffer", "torsocks", "proxychains"}
KEYWORDS = {"and", "or", "not", "!", "then", "do", "else", "elif", "if", "while", "until", "begin", "end",
            "fi", "done", "{", "}", "case", "esac", "function"}
# programs with single-dash long options: -name is one option, not -n -a -m -e
LONG_SINGLE_DASH = {"find", "java", "gcc", "g++", "clang", "ffmpeg", "ffprobe", "ffplay", "convert", "magick",
                    "mogrify", "xrandr", "qemu-system-x86_64", "mpv", "wine", "openssl", "iptables", "nft", "tc",
                    "ip", "xdotool", "go", "7z", "7za"}
SUBCOMMANDS = {"git", "systemctl", "apt", "apt-get", "dnf", "yum", "zypper", "flatpak", "snap", "docker", "podman",
               "npm", "pnpm", "yarn", "pip", "pip3", "cargo", "nmcli", "pactl", "kubectl", "gh", "ip", "journalctl",
               "loginctl", "timedatectl", "hostnamectl", "localectl", "bluetoothctl", "rfkill", "ufw", "firewall-cmd",
               "uv", "brew", "rustup", "npx", "conda", "ollama", "btrfs", "snapper", "flatpak-spawn"}
SYSTEM_DIRS = ("/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt", "/proc", "/root", "/sbin",
               "/srv", "/sys", "/usr", "/var")
DEVICE = re.compile(r"^/dev/(?:sd[a-z]|nvme\d|mmcblk\d|vd[a-z]|hd[a-z]|xvd[a-z]|disk/|mapper/|md\d|loop\d)")
URL = re.compile(r"https?://[^\s'\"<>|;)]+")
# files that are secrets: keys, passwords, browser profiles, cloud credentials
SECRET = re.compile(r"(?:\.ssh/|\.gnupg/|id_rsa|id_ed25519|id_ecdsa|\.password-store|/etc/shadow|\.mozilla/|"
                    r"\.config/(?:google-chrome|chromium|BraveSoftware|vivaldi)|\.aws/|\.kube/config|\.netrc|"
                    r"\.git-credentials|\.docker/config\.json|keyring|Cookies|Login Data|wallet)", re.I)
UPLOAD_FLAGS = {"-d", "--data", "--data-binary", "--data-raw", "--data-urlencode", "-F", "--form", "-T", "--upload-file",
                "--post-file", "--body-file"}


@dataclass
class Simple:
    """One simple command: `sudo rm -rf ~/x > log`."""
    argv: list[str]  # the program and its arguments, wrappers taken off
    wrappers: list[str] = field(default_factory=list)  # ["sudo"], ["env", "xargs"]…
    assigns: list[str] = field(default_factory=list)  # VAR=value before the program
    redirects: list[tuple[str, str]] = field(default_factory=list)  # (">", "file"), ("<", "file")
    subs: list["Parsed"] = field(default_factory=list)  # $( ), ( ), <( ) and sh -c '…' inside it
    piped_in: bool = False  # reads the previous command's output (… | this)

    @property
    def prog(self) -> str:
        return self.argv[0].rsplit("/", 1)[-1] if self.argv else ""

    @property
    def root(self) -> bool:
        return any(w in ("sudo", "doas", "pkexec", "run0") for w in self.wrappers)

    @property
    def sub(self) -> str:
        """The subcommand (git commit, systemctl enable), when the program has them."""
        if self.prog not in SUBCOMMANDS:
            return ""
        return next((a for a in self.argv[1:] if not a.startswith("-")), "")

    def flags(self) -> list[str]:
        out = []
        args = self.argv[1:]
        if self.prog == "tar" and args and re.fullmatch(r"[A-Za-z]+", args[0]):  # tar czf: old-style cluster
            out += [f"-{c}" for c in args[0]]
            args = args[1:]
        for a in args:
            if a == "--":
                break
            if a.startswith("--") and len(a) > 2:
                out.append(a.split("=", 1)[0])
            elif re.fullmatch(r"-[A-Za-z0-9]\S*", a):
                if self.prog in LONG_SINGLE_DASH or not re.fullmatch(r"-[A-Za-z]+", a):
                    out.append(a if self.prog in LONG_SINGLE_DASH else a[:2])
                else:
                    out += [f"-{c}" for c in a[1:]]
        return list(dict.fromkeys(out))

    def operands(self) -> list[str]:
        """Arguments that aren't options (paths, package names…)."""
        args, out, end = self.argv[1:], [], False
        for a in args:
            if a == "--" and not end:
                end = True
            elif end or not a.startswith("-") or a == "-":
                out.append(a)
        return out

    def text(self) -> str:
        return " ".join(self.wrappers + self.assigns + [shlex.quote(a) if re.search(r"\s", a) else a for a in self.argv])


@dataclass
class Pipeline:
    commands: list[Simple]
    then: str = ""  # what joins it to the next one: && || ; &


@dataclass
class Parsed:
    pipelines: list[Pipeline] = field(default_factory=list)
    error: str = ""

    def simples(self) -> list[Simple]:
        """Every simple command, nested substitutions included, in reading order."""
        out = []
        for p in self.pipelines:
            for c in p.commands:
                out.append(c)
                for s in c.subs:
                    out += s.simples()
        return out


# --- parsing ---------------------------------------------------------------------------------------

def _extract_subs(text: str) -> tuple[str, list[str]]:
    """Cuts out $( ), `…`, <( ), >( ) and fish ( ) (outside single quotes) and leaves placeholders,
    so shlex sees one word for each; returns the text and the inner commands."""
    out, subs, i, quote = [], [], 0, ""
    while i < len(text):
        c = text[i]
        if quote == "'":
            out.append(c)
            quote = "" if c == "'" else quote
            i += 1
            continue
        if c == "\\" and i + 1 < len(text):
            out.append(text[i:i + 2])
            i += 2
            continue
        if c in "'\"" and (not quote or quote == c):
            quote = "" if quote == c else c
            out.append(c)
            i += 1
            continue
        opener = text[i:i + 2]
        if opener in ("$(", "<(", ">(") or (c == "(" and not quote and (i == 0 or text[i - 1] in " \t|;&=")):
            start = i + (1 if c == "(" else 2)
            depth, j = 1, start
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            subs.append(text[start:j - 1])
            out.append(f"__SUB{len(subs) - 1}__")
            i = j
            continue
        if c == "`" and not quote == "'":
            j = text.find("`", i + 1)
            if j > 0:
                subs.append(text[i + 1:j])
                out.append(f"__SUB{len(subs) - 1}__")
                i = j + 1
                continue
        out.append(c)
        i += 1
    return "".join(out), subs


OPERATORS = {"|", "|&", "&&", "||", ";", "&", ";;"}
REDIRECT = re.compile(r"^(?:\d*|&)(?:>>?|<|<<<?|>&|&>>?|<&|>\|)$")


def parse(text: str, _depth: int = 0) -> Parsed:
    text = re.sub(r"\\\n", " ", text.strip())  # line continuations
    flat, sub_texts = _extract_subs(text)
    subs = [parse(s, _depth + 1) if _depth < 4 else Parsed() for s in sub_texts]
    lex = shlex.shlex(flat.replace("\n", " ; "), posix=True, punctuation_chars=";&|<>")
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        tokens = list(lex)
    except ValueError:
        return Parsed(error=t("незакрытая кавычка", "an unclosed quote"))
    # shlex splits "2>&1" into "2", ">&", "1": glue a file-descriptor number back to its operator
    glued = []
    for tok in tokens:
        if glued and REDIRECT.match(tok) and re.fullmatch(r"\d", glued[-1]):
            glued[-1] += tok
        else:
            glued.append(tok)
    result, pipe, cmd = Parsed(), [], []

    def close_cmd():
        if cmd:
            c = _simple(cmd, subs, _depth)
            c.piped_in = bool(pipe)
            pipe.append(c)
            cmd.clear()

    for tok in glued:
        if tok in OPERATORS:
            close_cmd()
            if tok in ("|", "|&"):
                continue
            if pipe:
                result.pipelines.append(Pipeline(list(pipe), tok))
                pipe.clear()
        else:
            cmd.append(tok)
    close_cmd()
    if pipe:
        result.pipelines.append(Pipeline(list(pipe)))
    return result


def _simple(tokens: list[str], subs: list[Parsed], depth: int) -> Simple:
    s = Simple([])
    words, i = [], 0
    while i < len(tokens):  # redirects first: "> file" anywhere in the command
        tok = tokens[i]
        if REDIRECT.match(tok) and i + 1 < len(tokens):
            s.redirects.append((tok, tokens[i + 1]))
            i += 2
            continue
        m = re.fullmatch(r"((?:\d*|&)(?:>>?|<))(\S+)", tok)
        if m and not tok.startswith("<("):
            s.redirects.append((m.group(1), m.group(2)))
        else:
            words.append(tok)
        i += 1
    # placeholders -> the substitutions they stand for
    for w in words + [target for _, target in s.redirects]:
        for n in re.findall(r"__SUB(\d+)__", w):
            if int(n) < len(subs):
                s.subs.append(subs[int(n)])
    while words and words[0] in KEYWORDS:
        words.pop(0)
    if words and words[0] in ("for", "case", "select"):  # a loop header, not a command
        return Simple([])
    while words:
        w = words[0]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w):
            s.assigns.append(words.pop(0))
            continue
        if w.rsplit("/", 1)[-1] not in WRAPPERS:
            break
        wrapper = words.pop(0).rsplit("/", 1)[-1]
        s.wrappers.append(wrapper)
        # the wrapper's own options and values (kept with it): sudo -u bob, timeout 10, env -i A=1, xargs -0 -I {}
        while words and (words[0].startswith("-") or re.fullmatch(r"[A-Za-z_]\w*=.*", words[0])
                         or (wrapper in ("timeout", "nice", "chrt", "taskset") and re.fullmatch(r"[\d.]+[smhd]?", words[0]))):
            opt = words.pop(0)
            s.wrappers.append(opt)
            takes_value = (wrapper in ("sudo", "doas") and opt in ("-u", "-g", "-C", "-p", "-h") or
                           wrapper == "xargs" and opt in ("-I", "-n", "-P", "-d", "-L", "-s", "-a", "-E") or
                           wrapper == "nice" and opt == "-n")
            if takes_value and words:
                s.wrappers.append(words.pop(0))
    s.argv = words
    # sh -c 'script' / bash -c "…": the string is a command too
    if s.prog in SHELLS and "-c" in words:
        k = words.index("-c")
        # sh -c "$(curl …)": the substitution is already among subs
        if k + 1 < len(words) and depth < 4 and not re.fullmatch(r"(?:__SUB\d+__\s*)+", words[k + 1]):
            s.subs.append(parse(words[k + 1], depth + 1))
    if s.prog == "eval" and depth < 4:
        s.subs.append(parse(" ".join(words[1:]), depth + 1))
    return s


# --- does a piece of text look like a command at all ------------------------------------------------

BUILTINS = {"cd", "export", "source", ".", "alias", "set", "unset", "echo", "printf", "read", "eval", "exec",
            "abbr", "funcsave", "bind", "ulimit", "umask", "history", "type", "which", "test", "["}


def looks_like_command(text: str, known=None) -> bool:
    """A pasted command rather than an error message or a question: the first word is a program
    (on PATH, in tldr, a shell builtin) and the line isn't prose. known(prog) -> bool checks a program."""
    text = text.strip()
    if not text or len(text) > 4000:
        return False
    letters = [c for c in text if c.isalpha()]
    if letters and sum("а" <= c.lower() <= "я" or c in "ёЁіїєґ" for c in letters) > len(letters) * 0.3:
        return False
    first_line = text.splitlines()[0]
    if re.match(r"^\s*(?:error|warning|fatal|e|w|err|ошибка)\s*:", first_line, re.I) or "Traceback" in text:
        return False
    p = parse(text)
    if p.error or not p.simples():
        return False
    first = p.simples()[0]
    if not first.argv:
        return False
    prog = first.prog
    if prog in BUILTINS or prog in WRAPPERS:
        return True
    if first.argv[0].startswith(("./", "/", "~/")):
        return True
    return bool(known and known(prog))


# --- risks -----------------------------------------------------------------------------------------

@dataclass
class Finding:
    level: str  # danger | caution | info
    text: str
    where: str = ""  # the simple command it is about
    url: str = ""  # a script that would be downloaded and run: it can be fetched and checked first


def _is_home(p: str) -> bool:
    return p.rstrip("/") in ("~", "$HOME", "${HOME}", "/home/$USER", "$HOME/*", "~/*") or p in ("~/", "~/*", "$HOME/")


def _dangerous_target(p: str) -> bool:
    bare = p.rstrip("/") or "/"
    if bare in ("/", "/*", "*", ".", "..", "./*", "~", "$HOME", "${HOME}") or _is_home(p):
        return True
    if bare in SYSTEM_DIRS or any(bare == d + "/*" for d in SYSTEM_DIRS):
        return True
    # "$DIR/" with an empty DIR is "/": the classic Steam-script bug
    return bool(re.match(r"^\$\{?\w+\}?/", p)) and not p.startswith(("$HOME/", "${HOME}/"))


def _download_inside(p: Parsed) -> list[str]:
    """URLs a substitution downloads: bash <(curl …), sh -c "$(wget -O- …)"."""
    return [u for c in p.simples() if c.prog in DOWNLOADERS for u in URL.findall(" ".join(c.argv))]


def risks(p: Parsed, raw: str = "") -> list[Finding]:
    out: list[Finding] = []
    add = lambda level, text, c=None, url="": out.append(Finding(level, text, c.text() if c else "", url))  # noqa: E731
    if re.search(r"(\w+|:)\(\)\s*\{\s*\1\s*\|\s*\1\s*&\s*\}", raw):
        add("danger", t("это «форк-бомба»: она запускает саму себя без конца, пока компьютер не зависнет",
                        "this is a “fork bomb”: it starts itself endlessly until the computer freezes"))
    for pl in _all_pipelines(p):
        cmds = pl.commands
        for i, c in enumerate(cmds[:-1]):
            later = [d for d in cmds[i + 1:] if d.prog in INTERPRETERS and not d.operands()[:1] or
                     d.prog in INTERPRETERS and d.operands()[:1] == ["-"] or d.prog in SHELLS and "-s" in d.argv]
            if c.prog in DOWNLOADERS and later:
                urls = URL.findall(" ".join(c.argv))
                add("danger", t(f"скачивает скрипт{' с ' + _hosts(urls) if urls else ''} и сразу запускает его"
                                f"{' от root' if any(d.root for d in later) else ''}, не показав тебе, что в нём",
                                f"downloads a script{' from ' + _hosts(urls) if urls else ''} and runs it right away"
                                f"{' as root' if any(d.root for d in later) else ''}, without showing you what's inside"), c,
                    urls[0] if urls else "")
            if (c.prog in ("base64", "xxd", "openssl") and ({"-d", "--decode", "-r"} & set(c.flags()) or "enc" in c.argv)
                    or c.prog in ("printf", "echo") and re.search(r"\\x[0-9a-f]{2}", " ".join(c.argv), re.I)) and later:
                add("danger", t("расшифровывает спрятанный текст и выполняет его как команды — так прячут вредоносный код",
                                "decodes hidden text and runs it as commands — a common way to hide malicious code"), c)
        senders = [c for c in cmds if c.prog in DOWNLOADERS | {"nc", "ncat", "socat"} and
                   (set(c.flags()) & UPLOAD_FLAGS or c.prog in ("nc", "ncat", "socat"))]
        secrets = [o for c in cmds for o in c.operands() + [x for _, x in c.redirects] if SECRET.search(o)]
        if senders and secrets:
            add("danger", t(f"отправляет секретные файлы ({', '.join(dict.fromkeys(secrets))}) на сервер "
                            f"{_hosts(URL.findall(' '.join(senders[0].argv))) or ''}",
                            f"sends secret files ({', '.join(dict.fromkeys(secrets))}) to the server "
                            f"{_hosts(URL.findall(' '.join(senders[0].argv))) or ''}"), senders[0])
    for c in p.simples():
        _rules(c, add)
    return _dedupe(out)


def _all_pipelines(p: Parsed) -> list[Pipeline]:
    out = list(p.pipelines)
    for pl in p.pipelines:
        for c in pl.commands:
            for s in c.subs:
                out += _all_pipelines(s)
    return out


def _hosts(urls: list[str]) -> str:
    return ", ".join(dict.fromkeys(re.sub(r"^https?://([^/:]+).*", r"\1", u) for u in urls))


def _rules(c: Simple, add) -> None:
    prog, flags, ops, args = c.prog, set(c.flags()), c.operands(), c.argv[1:]
    if not prog and not c.root:
        return
    # a script fed from a download through a substitution: bash <(curl …), sh -c "$(curl …)", eval "$(wget …)"
    if prog in INTERPRETERS | {"eval", "source", "."}:
        urls = [u for s in c.subs for u in _download_inside(s)]
        if urls:
            add("danger", t(f"запускает скрипт, скачанный с {_hosts(urls)}, не показав его",
                            f"runs a script downloaded from {_hosts(urls)} without showing it"), c, urls[0])
    if prog in DOWNLOADERS and set(c.flags()) & UPLOAD_FLAGS:
        sent = [a.lstrip("@") for a in args if a.startswith("@") or "=@" in a]
        sent = [a.split("=@", 1)[-1] for a in sent]
        if any(SECRET.search(a) for a in sent):
            add("danger", t(f"отправляет секретный файл ({', '.join(sent)}) на сервер", f"sends a secret file ({', '.join(sent)}) to a server"), c)
        elif sent and sent != ["-"]:
            add("info", t(f"отправляет файл на сервер: {', '.join(sent)}", f"uploads a file to a server: {', '.join(sent)}"), c)
    if prog == "rm":
        recursive = flags & {"-r", "-R", "--recursive"}
        bad = [o for o in ops if _dangerous_target(o)]
        if recursive and bad:
            add("danger", t(f"удалит безвозвратно {', '.join(bad)} целиком — со всеми файлами внутри",
                            f"permanently deletes all of {', '.join(bad)} — every file inside"), c)
        elif recursive:
            add("caution", t(f"удалит папки вместе с содержимым, мимо корзины: {', '.join(ops) or '—'}",
                             f"deletes folders with everything inside, bypassing the trash: {', '.join(ops) or '—'}"), c)
        elif ops:
            add("info", t(f"удалит файлы мимо корзины: {', '.join(ops)}", f"deletes files bypassing the trash: {', '.join(ops)}"), c)
        elif c.piped_in and "xargs" in c.wrappers:
            add("caution", t("удалит все файлы, которые выдала предыдущая команда, мимо корзины",
                             "deletes every file the previous command listed, bypassing the trash"), c)
        if "--no-preserve-root" in flags:
            add("danger", t("--no-preserve-root снимает защиту от удаления всей системы",
                            "--no-preserve-root removes the guard against deleting the whole system"), c)
    if prog == "dd" and any(a.startswith("of=") and DEVICE.match(a[3:]) for a in args):
        dev = next(a[3:] for a in args if a.startswith("of=") and DEVICE.match(a[3:]))
        add("danger", t(f"запишет прямо на диск {dev} — всё, что на нём было, пропадёт",
                        f"writes straight onto the disk {dev} — everything on it is lost"), c)
    if re.match(r"mkfs(\.\w+)?$", prog) or prog in ("wipefs", "blkdiscard", "shred", "sgdisk", "sfdisk", "mkswap") \
            or prog in ("fdisk", "cfdisk", "gdisk", "parted") and any(DEVICE.match(o) for o in ops):
        dev = next((o for o in ops if o.startswith("/dev/")), "")
        add("danger", t(f"меняет разделы или файловую систему{' на ' + dev if dev else ''}: данные на нём можно потерять",
                        f"changes partitions or the filesystem{' on ' + dev if dev else ''}: its data can be lost"), c)
    for op, target in c.redirects:
        if ">" in op and DEVICE.match(target):
            add("danger", t(f"пишет прямо в устройство {target} — данные на диске пропадут",
                            f"writes straight into the device {target} — the disk's data is lost"), c)
        elif "/dev/tcp/" in target or "/dev/udp/" in target:
            add("danger", t("открывает сетевое соединение из оболочки — так дают кому-то удалённый доступ к компьютеру",
                            "opens a network connection from the shell — this is how someone gets remote access"), c)
        elif op in (">", "1>", "&>", ">|") and _config_file(target):
            add("caution", t(f"перезапишет файл {target} целиком (старое содержимое пропадёт)",
                             f"overwrites the file {target} entirely (the old content is gone)"), c)
    if prog in ("chmod", "chown", "chgrp"):
        rec = flags & {"-R", "--recursive"}
        bad = [o for o in ops if _dangerous_target(o)]
        if rec and bad:
            add("danger", t(f"меняет права на всё внутри {', '.join(bad)} — система или вход в неё могут сломаться",
                            f"changes permissions of everything in {', '.join(bad)} — the system or logging in may break"), c)
        if prog == "chmod" and any(re.fullmatch(r"0?777|a\+rwx|o\+w", o) for o in ops):
            add("caution" if not rec else "danger",
                t("даёт всем пользователям право менять эти файлы", "lets every user change these files"), c)
    if prog == "nc" and flags & {"-e", "-c"} or prog in ("ncat", "socat") and "exec" in " ".join(args):
        add("danger", t("подключает оболочку к сети — так дают удалённый доступ к компьютеру",
                        "connects a shell to the network — this is how remote access is given away"), c)
    if prog in ("add-apt-repository", "apt-add-repository") or prog == "apt-key" and "add" in args \
            or prog in ("dnf", "yum") and "config-manager" in args or prog == "copr" or "copr" in args and "enable" in args \
            or any(_repo_file(target) for op, target in c.redirects if ">" in op) or prog == "tee" and any(_repo_file(o) for o in ops):
        add("caution", t("добавляет сторонний источник программ: его пакеты будут ставиться и обновляться как системные",
                         "adds a third-party software source: its packages install and update like the system's own"), c)
    if prog in ("pacman", "yay", "paru"):
        ops_flags = {f for f in flags if f.startswith("-") and not f.startswith("--")}
        if "-S" in ops_flags and "-y" in ops_flags and "-u" not in ops_flags and ops:
            add("caution", t("обновляет список пакетов, но не систему, и ставит пакет: на Arch это «частичное обновление», "
                             "после которого программы могут не запуститься. Лучше: -Syu",
                             "refreshes the package list but not the system, then installs: on Arch this is a “partial "
                             "upgrade” that can leave programs broken. Better: -Syu"), c)
        if "-R" in ops_flags and "-d" in ops_flags or "--nodeps" in flags:
            add("caution", t("удаляет пакет, не глядя на то, что от него зависит — другие программы могут сломаться",
                             "removes a package ignoring what depends on it — other programs may break"), c)
        if "--overwrite" in flags:
            add("caution", t("разрешает пакету перезаписать чужие файлы", "lets the package overwrite files owned by others"), c)
        if "-S" in ops_flags and "-c" in ops_flags and list(c.argv).count("c") + "".join(args).count("c") >= 2:
            add("info", t("очистит весь кэш пакетов — откатить обновление будет не из чего", "empties the whole package cache — nothing left to roll back to"), c)
    if prog in ("apt", "apt-get") and "--allow-remove-essential" in flags:
        add("danger", t("разрешает удалить пакеты, без которых система не работает", "allows removing packages the system can't work without"), c)
    if prog in ("dnf", "yum", "rpm") and "--nogpgcheck" in flags:
        add("caution", t("ставит пакеты без проверки подписи", "installs packages without checking their signature"), c)
    if prog == "systemctl" and c.sub in ("disable", "mask", "stop") and \
            any(re.match(r"(NetworkManager|systemd-networkd|sddm|gdm|lightdm|display-manager|dbus|systemd-logind|polkit)", o)
                for o in ops[1:]):
        add("caution", t("отключает службу, без которой может пропасть сеть или вход в систему",
                         "disables a service without which the network or logging in may stop working"), c)
    if prog in ("kill", "pkill") and ("-1" in ops or "-9" in flags and "-1" in args) or prog == "killall5":
        add("danger", t("завершит все твои процессы сразу — все программы закроются без сохранения",
                        "kills all your processes at once — every program closes without saving"), c)
    if prog == "crontab" and "-r" in flags:
        add("caution", t("удалит все задачи по расписанию (crontab) без вопроса", "deletes all scheduled tasks (crontab) without asking"), c)
    if prog == "git" and (c.sub == "reset" and "--hard" in flags or c.sub == "clean" and flags & {"-f", "--force"}
                          or c.sub == "push" and flags & {"-f", "--force"} or c.sub in ("checkout", "restore") and "." in ops):
        add("caution", t("несохранённые (незакоммиченные) изменения пропадут", "uncommitted changes are lost"), c)
    if prog in ("mv", "cp") and ops and ops[-1] == "/dev/null":
        add("danger", t("«перемещение» в /dev/null просто уничтожает файлы", "“moving” into /dev/null just destroys the files"), c)
    if prog in ("pip", "pip3") and c.root and "install" in args or "--break-system-packages" in flags:
        add("caution", t("ставит Python-пакеты поверх системных — может сломать программы из репозитория",
                         "installs Python packages over the system's — may break programs from the repositories"), c)
    for a in c.assigns + ([" ".join(args)] if prog in ("export", "set") else []):
        if re.match(r"(?:-\w+\s+)*LD_PRELOAD[= ]", a):
            add("caution", t("подгружает чужую библиотеку во все программы (LD_PRELOAD)", "loads a foreign library into every program (LD_PRELOAD)"), c)
        if re.match(r"(?:-\w+\s+)*PATH[= ]", a) and "$PATH" not in a and "$path" not in a:
            add("caution", t("заменяет PATH целиком — привычные команды перестанут находиться",
                             "replaces PATH entirely — ordinary commands stop being found"), c)
    if prog == "history" and "-c" in flags or prog == "unset" and "HISTFILE" in args:
        add("caution", t("стирает историю команд — так обычно заметают следы", "erases the command history — usually to cover tracks"), c)
    if any(len(m) >= 80 for m in re.findall(r"[A-Za-z0-9+/=]{80,}", " ".join(args))):
        add("caution", t("в команде длинный зашифрованный кусок (base64) — что в нём, не видно",
                         "the command contains a long encoded blob (base64) — you can't see what's in it"), c)
    if prog in ("shutdown", "reboot", "poweroff", "halt") or prog == "systemctl" and c.sub in ("reboot", "poweroff", "halt"):
        add("info", t("перезагрузит или выключит компьютер", "reboots or powers off the computer"), c)
    if c.root and (prog in SHELLS and not c.subs and not c.piped_in and not c.operands() or prog == "su") \
            or not c.argv and {"-i", "-s"} & set(c.wrappers) and c.root:
        add("info", t("открывает оболочку root: всё дальше выполняется с полными правами",
                      "opens a root shell: everything after it runs with full rights"), c)


def _config_file(path: str) -> bool:
    return path.startswith(("/etc/", "/boot/", "/usr/")) or bool(re.match(
        r"^(?:~|\$HOME)/\.(?:bashrc|bash_profile|profile|zshrc|zprofile|config/fish/config\.fish|ssh/|gnupg/)", path))


def _repo_file(path: str) -> bool:
    return bool(re.match(r"^/etc/(?:apt/sources\.list|yum\.repos\.d/|pacman\.conf|pacman\.d/|zypp/repos\.d/)", path))


def _dedupe(found: list[Finding]) -> list[Finding]:
    seen, out = set(), []
    for f in found:
        if (f.text, f.where) not in seen:
            seen.add((f.text, f.where))
            out.append(f)
    order = {"danger": 0, "caution": 1, "info": 2}
    return sorted(out, key=lambda f: order[f.level])


# --- what it changes -------------------------------------------------------------------------------

@dataclass
class Effects:
    root: bool = False
    writes: list[str] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)  # "install: obs-studio"
    services: list[str] = field(default_factory=list)  # "enable: sshd"
    network: list[str] = field(default_factory=list)  # hosts it talks to

    def empty(self) -> bool:
        return not (self.root or self.writes or self.deletes or self.packages or self.services or self.network)


INSTALL_WORDS = {"install", "add", "-S", "in"}
REMOVE_WORDS = {"remove", "uninstall", "purge", "erase", "-R", "rm", "autoremove"}
PKG_TOOLS = {"pacman", "yay", "paru", "apt", "apt-get", "dnf", "yum", "zypper", "flatpak", "snap", "pip", "pip3",
             "npm", "pnpm", "cargo", "brew", "pipx", "uv"}


def effects(p: Parsed) -> Effects:
    e = Effects()
    for c in p.simples():
        prog, ops, flags = c.prog, c.operands(), c.flags()
        e.root = e.root or c.root
        e.writes += [target for op, target in c.redirects if ">" in op and not target.startswith(("/dev/null", "&"))
                     and not re.fullmatch(r"&?\d", target)]
        if prog == "tee":
            e.writes += ops
        if prog in ("cp", "mv", "install", "ln", "rsync") and len(ops) >= 2:
            e.writes.append(ops[-1])
        if prog == "sed" and (set(flags) & {"-i", "--in-place"} or any(f.startswith("-i") for f in c.argv[1:])):
            e.writes += [o for o in ops[1:]]
        if prog in ("touch", "mkdir"):
            e.writes += ops
        if prog in ("rm", "rmdir", "unlink", "shred"):
            e.deletes += ops or ([t("файлы из вывода предыдущей команды", "the files the previous command listed")]
                                 if c.piped_in and "xargs" in c.wrappers else [])
        if prog == "dd":
            e.writes += [a[3:] for a in c.argv[1:] if a.startswith("of=")]
        if re.match(r"mkfs(\.\w+)?$", prog) or prog in ("wipefs", "sgdisk", "sfdisk", "parted", "fdisk", "mkswap"):
            e.writes += [o for o in ops if o.startswith("/dev/")]
        if prog == "find" and "-delete" in c.argv:
            e.deletes.append(t(f"файлы, найденные в {ops[0] if ops else '.'}", f"the files found in {ops[0] if ops else '.'}"))
        if prog in PKG_TOOLS:
            words = set(c.argv[1:]) | {f for f in flags if f in ("-S", "-R", "-U")}
            names = [o for o in ops if o not in INSTALL_WORDS | REMOVE_WORDS | {"-g", "global", "--global", "flathub", "fedora"}
                     and not o.startswith("-") and o != c.sub or prog in ("pacman", "yay", "paru") and not o.startswith("-")]
            pacman_query = prog in ("pacman", "yay", "paru") and not ({"-S", "-R", "-U"} & set(flags))
            if names and not pacman_query and words & INSTALL_WORDS:
                e.packages.append(t("установит: ", "installs: ") + ", ".join(names))
            elif names and not pacman_query and words & REMOVE_WORDS:
                e.packages.append(t("удалит: ", "removes: ") + ", ".join(names))
        if prog == "systemctl" and c.sub in ("enable", "disable", "start", "stop", "restart", "mask", "unmask"):
            e.services += [f"{c.sub}: {o}" for o in ops[1:]]
        if prog in DOWNLOADERS or prog == "git" and c.sub in ("clone", "pull", "fetch", "push"):
            e.network += [re.sub(r"^https?://([^/:]+).*", r"\1", u) for u in URL.findall(" ".join(c.argv))]
    for name in ("writes", "deletes", "packages", "services", "network"):
        setattr(e, name, list(dict.fromkeys(getattr(e, name))))
    return e


def script_lines(text: str) -> list[tuple[int, str]]:
    """(line number, command) of a script, comments and blank lines dropped, continuations joined."""
    out, buf, start = [], "", 0
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not buf and (not s or s.startswith("#")):
            continue
        if not buf:
            start = n
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        out.append((start, buf + s))
        buf = ""
    if buf:
        out.append((start, buf))
    return out
