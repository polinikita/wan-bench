"""Parallel SSH and SCP through system clients."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import shlex
import subprocess


@dataclasses.dataclass
class Host:
    """A host with public control and private data-plane addresses."""

    index: int
    instance_id: str
    public_ip: str
    private_ip: str


_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=15",
    "-o", "LogLevel=ERROR",
    # Reuse authenticated connections across benchmark steps.
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=/tmp/wanbench-%r@%h:%p",
    "-o", "ControlPersist=60s",
]


class Ssh:
    def __init__(self, key_path: str, user: str):
        self.key_path = key_path
        self.user = user

    def _base(self) -> list[str]:
        return ["ssh", "-i", self.key_path, *_SSH_OPTS]

    def run(self, host: Host, command: str, check: bool = True, timeout: int = 120) -> str:
        """Run a shell command on `host`, return stdout. Raises on nonzero if check."""
        target = f"{self.user}@{host.public_ip}"
        proc = subprocess.run(
            [*self._base(), target, command],
            capture_output=True, text=True, timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"[node {host.index} {host.public_ip}] `{command}` rc={proc.returncode}\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        return proc.stdout

    def sudo(self, host: Host, command: str, **kw) -> str:
        return self.run(host, f"sudo bash -lc {shlex.quote(command)}", **kw)

    def scp(self, host: Host, local: str, remote: str, timeout: int = 120) -> None:
        target = f"{self.user}@{host.public_ip}:{remote}"
        proc = subprocess.run(
            ["scp", "-i", self.key_path, *_SSH_OPTS, local, target],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"scp -> node {host.index}: {proc.stderr}")

    def fetch(self, host: Host, remote: str, local: str, timeout: int = 120) -> None:
        """Copy one remote file to a local path."""
        source = f"{self.user}@{host.public_ip}:{remote}"
        proc = subprocess.run(
            ["scp", "-i", self.key_path, *_SSH_OPTS, source, local],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"scp <- node {host.index}: {proc.stderr}")

    def fanout(self, hosts: list[Host], command, max_workers: int = 32) -> list:
        """Run a command in parallel and return results in host order."""
        def one(h: Host):
            cmd = command(h) if callable(command) else command
            return self.run(h, cmd)

        return self.parallel(hosts, one, max_workers)

    def parallel(self, hosts: list[Host], action, max_workers: int = 32) -> list:
        """Run a host action in parallel and preserve host order."""
        if not hosts:
            return []

        out: list = [None] * len(hosts)
        workers = min(max_workers, len(hosts))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(action, h): i for i, h in enumerate(hosts)}
            for fut in concurrent.futures.as_completed(futs):
                out[futs[fut]] = fut.result()
        return out

    def wait_ready(self, hosts: list[Host], attempts: int = 40, delay: int = 5) -> None:
        """Poll hosts concurrently until each accepts SSH."""
        import time

        # Retain the last error for each unresolved host.
        last_error: dict[int, str] = {}

        def probe(h: Host) -> Host | None:
            try:
                self.run(h, "true", timeout=15)
                return None
            except Exception as exc:  # noqa: BLE001 -- recorded, then retried
                last_error[h.index] = f"{type(exc).__name__}: {exc}"
                return h

        pending = list(hosts)
        for _ in range(attempts):
            workers = min(32, max(1, len(pending)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                still = [h for h in pool.map(probe, pending) if h is not None]
            if not still:
                return
            pending = still
            time.sleep(delay)
        detail = "; ".join(
            f"node {h.index} ({h.public_ip}) {last_error.get(h.index, 'no error recorded')}"
            for h in pending)
        raise TimeoutError(
            f"{len(pending)} host(s) never became SSH-ready after "
            f"{attempts} attempts x {delay}s: {detail}")
