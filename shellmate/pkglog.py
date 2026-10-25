"""Package transactions from the package manager's own log: what changed, from which version to which.
Pure parsing, used by the backends (Backend.transactions) and by afterupdate.py."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class Change:
    name: str
    old: str  # "" when installed fresh (or unknown, rpm)
    new: str  # "" when removed
    action: str  # upgraded | downgraded | installed | removed | reinstalled


@dataclass
class Txn:
    time: datetime
    changes: list[Change] = field(default_factory=list)

    @property
    def upgrades(self) -> list[Change]:
        return [c for c in self.changes if c.action in ("upgraded", "downgraded")]


PACMAN_LINE = re.compile(r"^\[([^\]]+)\] \[ALPM\] (upgraded|downgraded|installed|removed|reinstalled) "
                         r"(\S+) \((.+?)\)\s*$")


def _when(stamp: str) -> datetime | None:
    stamp = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", stamp.replace("Z", "+00:00").replace(" ", "T"))  # Python 3.10: "+03:00"
    try:  # 2026-10-20T11:33:32+0300, or the old "2019-03-01 10:00" format
        return datetime.fromisoformat(stamp).astimezone()
    except ValueError:
        return None


def parse_pacman_log(text: str) -> list[Txn]:
    """pacman.log: "[ALPM] transaction started" … "[ALPM] upgraded mesa (1-1 -> 2-1)" … completed."""
    txns, cur = [], None
    for line in text.splitlines():
        if "[ALPM] transaction started" in line:
            m = re.match(r"^\[([^\]]+)\]", line)
            when = _when(m.group(1)) if m else None
            cur = Txn(when) if when else None
            continue
        if "[ALPM] transaction completed" in line or "[ALPM] transaction failed" in line:
            if cur and cur.changes:
                txns.append(cur)
            cur = None
            continue
        m = PACMAN_LINE.match(line)
        if not m:
            continue
        if cur is None:  # a log without transaction markers (old pacman): one per minute
            when = _when(m.group(1))
            if not when:
                continue
            if txns and when - txns[-1].time < timedelta(minutes=2):
                cur_t = txns[-1]
            else:
                cur_t = Txn(when)
                txns.append(cur_t)
        else:
            cur_t = cur
        action, name, ver = m.group(2), m.group(3), m.group(4)
        old, _, new = ver.partition(" -> ")
        if action in ("installed", "reinstalled"):
            old, new = "", old
        elif action == "removed":
            new = ""
        cur_t.changes.append(Change(name, old, new, action))
    if cur and cur.changes:
        txns.append(cur)
    return txns


APT_ITEM = re.compile(r"([^\s,()]+?)(?::\w+)? \(([^)]*)\)")


def parse_apt_history(text: str) -> list[Txn]:
    """/var/log/apt/history.log blocks: Start-Date, then "Upgrade: pkg:arch (old, new), …"."""
    txns = []
    for block in text.split("\n\n"):
        m = re.search(r"^Start-Date: (\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})", block, re.M)
        if not m:
            continue
        txn = Txn(datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}").astimezone())
        for kind, action in (("Upgrade", "upgraded"), ("Downgrade", "downgraded"), ("Install", "installed"),
                             ("Remove", "removed"), ("Purge", "removed"), ("Reinstall", "reinstalled")):
            line = re.search(rf"^{kind}: (.*)$", block, re.M)
            if not line:
                continue
            for name, vers in APT_ITEM.findall(line.group(1)):
                parts = [v.strip() for v in vers.split(",")]
                if action in ("upgraded", "downgraded"):
                    txn.changes.append(Change(name, parts[0], parts[1] if len(parts) > 1 else "", action))
                elif action == "removed":
                    txn.changes.append(Change(name, parts[0], "", action))
                else:
                    txn.changes.append(Change(name, "", parts[0], action))
        if txn.changes:
            txns.append(txn)
    return txns


def parse_rpm_installtimes(text: str) -> list[Txn]:
    """`rpm -qa --qf '%{INSTALLTIME} %{NAME} %{VERSION}-%{RELEASE}\\n'`: rpm keeps when each package was
    last installed or upgraded, not the old version; packages within 10 minutes form one transaction."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1], parts[2]))
    rows.sort()
    txns: list[Txn] = []
    last = None
    for ts, name, ver in rows:
        if last is None or ts - last > 600:
            txns.append(Txn(datetime.fromtimestamp(ts).astimezone()))
        txns[-1].changes.append(Change(name, "", ver, "upgraded"))
        last = ts
    return txns
