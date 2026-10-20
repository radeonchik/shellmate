"""Runs a command through a pseudo-terminal: the user can still answer prompts
(sudo password, paru [Y/n]) while we record the output to diagnose failures."""

import os
import pty
import shutil

from shellmate.backends.base import ANSI

KEEP = 64_000  # only the tail matters for error diagnosis


def run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    buf = bytearray()

    def read(fd: int) -> bytes:
        data = os.read(fd, 4096)
        buf.extend(data)
        if len(buf) > 2 * KEEP:
            del buf[:-KEEP]
        return data

    # must check up front: an exec failure inside pty.spawn's forked child is not catchable here
    if not shutil.which(cmd[0]):
        return 127, f"{cmd[0]}: command not found"
    if env:  # apt/dnf get the C locale for stable messages; after sudo, which resets the environment
        assign = ["env", *[f"{k}={v}" for k, v in env.items()]]
        cmd = ["sudo", *assign, *cmd[1:]] if cmd[0] == "sudo" else [*assign, *cmd]
    status = pty.spawn(cmd, read)
    out = ANSI.sub("", buf.decode(errors="replace")).replace("\r\n", "\n")
    return os.waitstatus_to_exitcode(status), out[-KEEP:]
