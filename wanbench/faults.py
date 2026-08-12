"""Inject crash and private-network faults into validators."""

from __future__ import annotations

import math

from .config import RunConfig
from .protocols import VANTAGE_PORTS, uses_vantage_ports
from .ssh import Host, Ssh

_CHAIN = "WANBENCH"


def _reject_flag(mode: str) -> str:
    if mode not in ("cut", "drop"):
        raise ValueError(f"fault mode must be cut or drop, got {mode!r}")
    return "-j REJECT --reject-with tcp-reset" if mode == "cut" else "-j DROP"


def _chain_reset(ssh: Ssh, host: Host) -> str:
    return (f"iptables -D OUTPUT -j {_CHAIN} 2>/dev/null || true; "
            f"iptables -F {_CHAIN} 2>/dev/null || true; "
            f"iptables -X {_CHAIN} 2>/dev/null || true; "
            f"iptables -N {_CHAIN}; iptables -I OUTPUT 1 -j {_CHAIN}")


def _peer_ports(cfg: RunConfig, target: Host) -> tuple[int, ...]:
    if uses_vantage_ports(cfg.protocol):
        return (VANTAGE_PORTS["consensus_to_consensus"],
                VANTAGE_PORTS["primary_to_primary"],
                VANTAGE_PORTS["worker_to_worker"])
    return (1500 + target.index,)


def _install(ssh: Ssh, cfg: RunConfig, host: Host, targets: list[Host], mode: str) -> None:
    body = _chain_reset(ssh, host)
    for target in targets:
        for port in _peer_ports(cfg, target):
            body += (f"; iptables -A {_CHAIN} -d {target.private_ip}/32 -p tcp "
                     f"--dport {port} {_reject_flag(mode)}")
    ssh.sudo(host, body)


def _uninstall_all(ssh: Ssh, hosts: list[Host]) -> None:
    def clear(h: Host) -> str:
        return (f"sudo bash -lc '"
                f"iptables -D OUTPUT -j {_CHAIN} 2>/dev/null || true; "
                f"iptables -F {_CHAIN} 2>/dev/null || true; "
                f"iptables -X {_CHAIN} 2>/dev/null || true'")
    ssh.fanout(hosts, clear)


def crash(ssh: Ssh, hosts: list[Host], nodes: list[int]) -> None:
    unknown = sorted(set(nodes) - {h.index for h in hosts})
    if unknown:
        raise ValueError(f"unknown crash node indices: {unknown}")
    dead = [h for h in hosts if h.index in set(nodes)]
    ssh.fanout(dead, "sudo docker kill --signal=KILL wanbench-node")
    print(f"fault: crash-stopped nodes {sorted(nodes)}")


def restart(ssh: Ssh, hosts: list[Host], nodes: list[int]) -> None:
    """Restart crash-stopped validators in place (`docker start`, state kept)."""
    unknown = sorted(set(nodes) - {h.index for h in hosts})
    if unknown:
        raise ValueError(f"unknown restart node indices: {unknown}")
    dead = [h for h in hosts if h.index in set(nodes)]
    # The vantage entrypoint truncates its logs on start; rotate them so the
    # pre-crash evidence survives. Starfish images have no logs directory.
    cmd = ("sudo bash -lc '"
           "if [ -d /opt/wanbench/logs ]; then "
           "cd /opt/wanbench/logs && "
           "for f in *.log; do [ -e \"$f\" ] && mv \"$f\" \"$f.pre-restart\"; done; "
           "fi; "
           "docker start wanbench-node'")
    ssh.fanout(dead, cmd)
    print(f"fault: restarted nodes {sorted(nodes)}")


def ring(ssh: Ssh, cfg: RunConfig, hosts: list[Host], pct: int, mode: str = "cut") -> None:
    n = len(hosts)
    if n < 2:
        raise ValueError("ring fault needs at least 2 nodes")
    if not 1 <= pct <= 100:
        raise ValueError(f"ring percentage must be between 1 and 100, got {pct}")
    _reject_flag(mode)
    k = max(1, math.ceil((n - 1) * pct / 100))
    by_index = {h.index: h for h in hosts}
    for h in hosts:
        targets = [by_index[(h.index + i) % n] for i in range(1, k + 1)]
        _install(ssh, cfg, h, targets, mode)
    print(f"fault: ring cut K={k} ({pct}%) on {n} nodes, mode={mode}")


def split(ssh: Ssh, cfg: RunConfig, hosts: list[Host],
          group_a: list[int], group_b: list[int],
          mode: str = "cut") -> None:
    a, b = set(group_a), set(group_b)
    by_index = {h.index: h for h in hosts}
    unknown = sorted((a | b) - set(by_index))
    if unknown:
        raise ValueError(f"unknown split node indices: {unknown}")
    if not a or not b or a & b:
        raise ValueError("split groups must be non-empty and disjoint")
    _reject_flag(mode)
    for h in hosts:
        if h.index in a:
            targets = [by_index[j] for j in b]
        elif h.index in b:
            targets = [by_index[j] for j in a]
        else:
            continue
        _install(ssh, cfg, h, targets, mode)
    print(f"fault: split {sorted(a)} | {sorted(b)}, mode={mode}")


def blip_on(ssh: Ssh, cfg: RunConfig, hosts: list[Host],
            node: int, peers: list[int] | None,
            mode: str = "cut") -> None:
    by_index = {h.index: h for h in hosts}
    unknown = ({node, *(peers or [])} - set(by_index))
    if unknown:
        raise ValueError(f"unknown blip node indices: {sorted(unknown)}")
    _reject_flag(mode)
    me = by_index[node]
    others = peers if peers is not None else [h.index for h in hosts if h.index != node]
    targets = [by_index[j] for j in others]
    _install(ssh, cfg, me, targets, mode)
    print(f"fault: blip node {node} vs {others}, mode={mode}")


def clear(ssh: Ssh, hosts: list[Host]) -> None:
    """Remove every wan-bench fault rule on every node (idempotent)."""
    _uninstall_all(ssh, hosts)
    print("fault: cleared on all nodes")


def apply_from_config(ssh: Ssh, cfg: RunConfig, hosts: list[Host]) -> None:
    """Apply the configured fault; the caller clears timed faults."""
    f = cfg.fault
    if f.kind == "none":
        return
    if f.kind == "crash":
        crash(ssh, hosts, f.nodes)
    elif f.kind == "ring":
        ring(ssh, cfg, hosts, f.pct, f.mode)
    elif f.kind == "split":
        split(ssh, cfg, hosts, f.group_a, f.group_b, f.mode)
    elif f.kind == "blip":
        blip_on(ssh, cfg, hosts, f.nodes[0] if f.nodes else 0, None, f.mode)
