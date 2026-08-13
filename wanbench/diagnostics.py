"""Capture bounded post-measurement node diagnostics."""

from __future__ import annotations

import json
import pathlib
import re
import shlex
import statistics

from .ssh import Host, Ssh


_SCRIPT = r"""
set +e
section() { printf '\n[%s]\n' "$1"; }
section docker-state
docker inspect --format '{{json .State}}' wanbench-node 2>&1
section docker-stats
docker stats --no-stream --format '{{json .}}' wanbench-node 2>&1
section primary-tcp
ss -Htin state established '( sport = :6001 or dport = :6001 )' 2>&1
section worker-tcp
ss -Htin state established '( sport = :6006 or dport = :6006 )' 2>&1
section socket-summary
ss -s 2>&1
section netem
tc -s qdisc show 2>&1
section network-counters
sed -n '1,80p' /proc/net/snmp /proc/net/netstat 2>&1
section memory
free -b 2>&1
section disk
df -B1 /opt/wanbench 2>&1
section log-sizes
wc -lc /opt/wanbench/logs/primary.log /opt/wanbench/logs/worker0.log \
  /opt/wanbench/logs/client.log /opt/wanbench/logs/adversarial-client.log 2>&1
section primary-log-events
timeout 20 grep -Ei 'warn|error|panic|sequence (sync|install)|connection closed|failed to' /opt/wanbench/logs/primary.log 2>&1 | tail -n 5000
section primary-log-head
head -n 1000 /opt/wanbench/logs/primary.log 2>&1
section primary-log-tail
tail -n 3000 /opt/wanbench/logs/primary.log 2>&1
section worker-log-events
timeout 20 grep -Ei 'warn|error|panic|connection closed|failed to' /opt/wanbench/logs/worker0.log 2>&1 | tail -n 5000
section worker-log-tail
tail -n 3000 /opt/wanbench/logs/worker0.log 2>&1
section client-log-tail
tail -n 1000 /opt/wanbench/logs/client.log 2>&1
section adversarial-client-log-tail
tail -n 1000 /opt/wanbench/logs/adversarial-client.log 2>&1
section docker-log-tail
timeout 20 docker logs --tail 1000 wanbench-node 2>&1
"""


def _section(text: str, name: str) -> str:
    match = re.search(
        rf"^\[{re.escape(name)}\]\n(.*?)(?=^\[[^\n]+\]\n|\Z)",
        text,
        re.M | re.S,
    )
    return match.group(1) if match else ""


def _tcp_summary(text: str, section: str) -> dict:
    body = _section(text, section)
    rtts = [float(value) for value in re.findall(r"\brtt:([0-9.]+)/", body)]
    peers = set()
    sessions = 0
    for line in body.splitlines():
        if not line or line[0].isspace():
            continue
        fields = line.split()
        if len(fields) < 4 or ":" not in fields[2] or ":" not in fields[3]:
            continue
        sessions += 1
        peers.add(fields[3].rsplit(":", 1)[0].strip("[]"))
    return {
        "mean_rtt_ms": round(statistics.fmean(rtts), 3) if rtts else None,
        "rtt_samples": len(rtts),
        "sessions": sessions,
        "peer_ips": len(peers),
    }


def _write_network_summary(out: pathlib.Path, tag: str, hosts: list[Host]) -> None:
    rows = []
    for host in hosts:
        path = out / f"{tag}-diagnostics-node-{host.index}.txt"
        text = path.read_text() if path.exists() else ""
        rows.append({
            "node": host.index,
            "primary": _tcp_summary(text, "primary-tcp"),
            "worker": _tcp_summary(text, "worker-tcp"),
        })
    (out / f"{tag}-network-rtt.json").write_text(json.dumps(rows, indent=2))


def capture_nodes(ssh: Ssh, hosts: list[Host], outdir: str | pathlib.Path,
                  tag: str = "final") -> int:
    """Write one bounded diagnostic file per node without failing the run."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", tag):
        raise ValueError(f"invalid diagnostic tag {tag!r}")
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    command = f"sudo bash -lc {shlex.quote(_SCRIPT)}"

    def one(host: Host) -> str:
        try:
            return ssh.run(host, command, timeout=90, check=False)
        except Exception as exc:  # noqa: BLE001 -- diagnostics are best effort
            return f"[capture-error]\n{type(exc).__name__}: {exc}\n"

    texts = ssh.parallel(hosts, one, max_workers=16)
    complete = 0
    for host, body in zip(hosts, texts):
        (out / f"{tag}-diagnostics-node-{host.index}.txt").write_text(body)
        complete += "[capture-error]" not in body
    _write_network_summary(out, tag, hosts)
    print(f"collect: diagnostics {complete}/{len(hosts)} -> {out}", flush=True)
    return complete
