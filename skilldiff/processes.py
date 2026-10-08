"""Remember observed descendants so a server's new session cannot evade cleanup."""

import os
import signal
import subprocess
import threading


def process_table() -> dict[int, tuple[int, str]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,lstart="], capture_output=True, text=True, timeout=2, check=False
    )
    if result.returncode:
        return {}
    table = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3:
            table[int(fields[0])] = (int(fields[1]), fields[2])
    return table


class OwnedDescendants:
    def __init__(self, pid: int):
        self.pid = pid
        self.known: dict[int, str] = {}
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        if os.name == "posix" and type(pid) is int and pid > 1:
            self.thread = threading.Thread(target=self._watch, daemon=True)
            self.thread.start()

    def _watch(self) -> None:
        while not self.stop.is_set():
            try:
                table = process_table()
                if self.pid in table and self.pid not in self.known:
                    self.known[self.pid] = table[self.pid][1]
                # Identity includes launch time, preventing unrelated reused PIDs
                # from becoming parents in the ownership chain.
                parents = {
                    pid
                    for pid, birth in self.known.items()
                    if pid in table and table[pid][1] == birth
                }
                while True:
                    children = {
                        pid
                        for pid, (parent, birth) in table.items()
                        if parent in parents and pid not in parents
                    }
                    if not children:
                        break
                    for pid in children:
                        self.known[pid] = table[pid][1]
                    parents.update(children)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass  # The process-group boundary still applies if ps is unavailable.
            self.stop.wait(0.25)

    def cleanup(self) -> None:
        self.stop.set()
        if not self.thread:
            return
        self.thread.join(timeout=3)
        try:
            table = process_table()
            for pid, birth in list(self.known.items()):
                if pid != self.pid and pid in table and table[pid][1] == birth:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
