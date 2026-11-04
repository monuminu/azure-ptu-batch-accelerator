"""Run ACA's interactive exec in a PTY and verify the durable drain marker.

`az containerapp exec` expects a terminal even when `--command` is supplied.
This wrapper lets noninteractive azd hooks verify the command's output.
"""

from __future__ import annotations

import argparse
import errno
import os
import pty
import re
import select
import signal
import time

_MARKER = "PTU_BATCH_DRAIN_SAFE"
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--resource-group", required=True)
    args = parser.parse_args()

    command = [
        "az",
        "containerapp",
        "exec",
        "--name",
        args.name,
        "--resource-group",
        args.resource_group,
        "--container",
        "control",
        "--command",
        "ptu-batch maintenance --check",
        "--only-show-errors",
    ]
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(command[0], command)

    deadline = time.monotonic() + 90
    output = bytearray()
    status = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 1)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 1024 * 1024:
                    raise RuntimeError("maintenance exec output exceeded 1 MiB")
            if status is None:
                child, child_status = os.waitpid(pid, os.WNOHANG)
                if child:
                    status = child_status
        else:
            raise TimeoutError("maintenance exec did not finish in 90 seconds")
    finally:
        os.close(fd)
        if status is None:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            _, status = os.waitpid(pid, 0)

    clean = _ANSI.sub("", output.decode("utf-8", errors="replace"))
    if os.waitstatus_to_exitcode(status) != 0 or _MARKER not in {
        line.strip() for line in clean.replace("\r", "").splitlines()
    }:
        raise SystemExit("Control maintenance check failed or did not return PTU_BATCH_DRAIN_SAFE")
    print(_MARKER)


if __name__ == "__main__":
    main()
