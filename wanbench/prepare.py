"""Prepare instances, images, storage, and WAN shaping."""

from __future__ import annotations

import csv
import pathlib
import re

from .config import RunConfig
from .ssh import Host, Ssh

# Install Docker CE because protocol builds require BuildKit.
_INSTALL = r"""
set -euo pipefail

# Wait for cloud-init before modifying apt state.
cloud-init status --wait >/dev/null 2>&1 || true
systemctl stop apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.timer 2>/dev/null || true
systemctl mask apt-daily.service apt-daily-upgrade.service unattended-upgrades.service 2>/dev/null || true
# Match exact process names so this shell is not selected.
for p in apt-get dpkg unattended-upgrade unattended-upgrades; do
  pkill -9 -x "$p" 2>/dev/null || true
done
rm -f /var/lib/apt/lists/lock /var/lib/dpkg/lock /var/lib/dpkg/lock-frontend \
      /var/cache/apt/archives/lock 2>/dev/null || true
dpkg --configure -a >/dev/null 2>&1 || true

if ! docker buildx version >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
  # Retry transient package or mirror failures.
  ok=""
  for attempt in 1 2 3; do
    if curl -fsSL https://get.docker.com | sh && \
       apt-get install -yqq iproute2 iptables; then
      ok=1; break
    fi
    echo "docker install attempt $attempt failed; retrying" >&2
    sleep 10
    apt-get -qq update >/dev/null 2>&1 || true
  done
  [ -n "$ok" ] || { echo "docker install failed after 3 attempts" >&2; exit 1; }
  systemctl enable --now docker
fi
docker buildx version
mkdir -p /opt/wanbench
"""


def _rtt_table(cfg: RunConfig) -> list[list[float]]:
    rows: list[list[float]] = []
    path = pathlib.Path(__file__).resolve().parent.parent / cfg.wan.rtt_csv
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append([float(x) for x in line.split(",")])
    return rows


def one_way_ms(cfg: RunConfig, i: int, j: int) -> float:
    table = _rtt_table(cfg)
    m = len(table)
    rtt = table[i % m][j % m]
    return rtt / 2.0 if cfg.wan.halve_rtt else rtt


def install_docker(ssh: Ssh, hosts: list[Host]) -> None:
    ssh.fanout(hosts, lambda h: f"sudo bash -lc {_shq(_INSTALL)}")


def arm_deadman(ssh: Ssh, hosts: list[Host], minutes: int, quiet: bool = False) -> None:
    """Arm or renew each host's automatic termination deadline."""
    if minutes <= 0:
        return
    cmd = (f"shutdown -c >/dev/null 2>&1 || true; "
           f"shutdown -h +{minutes} 'wanbench dead-man switch' >/dev/null 2>&1 || true")
    try:
        ssh.fanout(hosts, lambda h: f"sudo bash -lc {_shq(cmd)}")
        if not quiet:
            print(f"prepare: dead-man switch armed on {len(hosts)} host(s): "
                  f"self-terminate in {minutes} min unless renewed", flush=True)
    except Exception as e:  # noqa: BLE001 - see docstring: never fatal
        print(f"prepare: WARNING could not arm the dead-man switch: {e}", flush=True)


def tune_control_sshd(ssh: Ssh, control: Host) -> None:
    """Raise the control host's concurrent SSH limits."""
    ssh.sudo(control,
             "printf 'MaxStartups 128\\nMaxSessions 128\\n' "
             "> /etc/ssh/sshd_config.d/90-wanbench.conf && "
             "systemctl reload ssh || systemctl reload sshd")


# Mount local instance storage at /opt/wanbench when available.
_MOUNT_NVME = r"""
set -euo pipefail
MP=/opt/wanbench
mkdir -p "$MP"
# Reuse an existing mount before scanning unmounted devices.
if mountpoint -q "$MP"; then
  echo "instance-store: $MP already mounted from $(findmnt -no SOURCE "$MP") ($(df -h --output=size "$MP" | tail -1 | tr -d ' ')) -- reusing"
else
  # Exclude the root disk.
  ROOTPART="$(findmnt -no SOURCE / | sed 's:^/dev/::')"
  ROOTDISK="$(lsblk -no pkname "/dev/$ROOTPART" 2>/dev/null | head -1)"
  [ -z "$ROOTDISK" ] && ROOTDISK="$ROOTPART"
  # Select the first unmounted non-root disk.
  DEV="$(lsblk -dpno NAME,TYPE,MOUNTPOINT | awk -v r="/dev/$ROOTDISK" '$2=="disk" && $3=="" && $1!=r {print $1; exit}')"
  if [ -n "$DEV" ]; then
    blkid "$DEV" >/dev/null 2>&1 || mkfs.ext4 -F -q "$DEV"
    mount "$DEV" "$MP"
    echo "instance-store: mounted $DEV at $MP ($(df -h --output=size "$MP" | tail -1 | tr -d ' '))"
  else
    echo "instance-store: no local NVMe found; $MP stays on the root volume"
  fi
fi
mkdir -p "$MP"
# Restore write access for the SSH user.
chown "${SUDO_UID:-$(id -u)}" "$MP"
"""


def mount_instance_store(ssh: Ssh, hosts: list[Host]) -> None:
    """Mount local NVMe storage at /opt/wanbench."""
    out = ssh.fanout(hosts, lambda h: f"sudo bash -lc {_shq(_MOUNT_NVME)}")
    print(f"prepare: {out[0].strip().splitlines()[-1] if out and out[0].strip() else 'nvme step done'}")


def pull_image(ssh: Ssh, hosts: list[Host], image: str, ghcr_token: str | None = None) -> None:
    login = ""
    if ghcr_token:
        # Keep the token out of Docker arguments.
        login = (f"echo {ghcr_token} | docker login ghcr.io "
                 f"-u wan-bench --password-stdin >/dev/null 2>&1; ")
    # Retry transient registry failures on each host.
    pull = (f"for i in 1 2 3; do {login}docker pull {image} && exit 0; "
            f'echo "pull attempt $i failed; retrying" >&2; sleep 10; done; exit 1')
    ssh.fanout(hosts, lambda h: f"sudo bash -lc {_shq(pull)}")


def tc_script(cfg: RunConfig, hosts: list[Host], me: int) -> str:
    """Generate idempotent, private-IP tc rules for one validator."""
    limit = max(1, int(cfg.wan.netem_limit_pkts))
    lines = [
        "set -euo pipefail",
        'IFACE="$(ip -o -4 route show to default | awk \'{print $5; exit}\')"',
        '[ -n "$IFACE" ] || { echo "no default iface" >&2; exit 0; }',
        'tc qdisc del dev "$IFACE" root >/dev/null 2>&1 || true',
        'tc qdisc add dev "$IFACE" root handle 1: htb default 999',
        'tc class add dev "$IFACE" parent 1: classid 1:999 htb rate 10gbit quantum 60000',
        # Hash by the destination IP's last octet; unmatched traffic is unshaped.
        'tc filter add dev "$IFACE" parent 1:0 prio 1 handle 2: protocol ip u32 divisor 256',
        'tc filter add dev "$IFACE" parent 1:0 prio 1 protocol ip u32 '
        'match ip dst 0.0.0.0/0 hashkey mask 0x000000ff at 16 link 2:',
    ]
    for h in hosts:
        if h.index == me:
            continue
        delay = one_way_ms(cfg, me, h.index)
        jit = f" {cfg.wan.jitter_ms:.1f}ms" if cfg.wan.jitter_ms else ""
        mid = 100 + h.index
        # u32 bucket IDs are hexadecimal.
        bucket = f"{int(h.private_ip.split('.')[-1]):x}"
        lines += [
            f"# node {me} -> node {h.index} ({h.private_ip}): {delay:.1f} ms one-way",
            f'tc class add dev "$IFACE" parent 1: classid 1:{mid} htb rate 10gbit quantum 60000',
            # netem requires the queue limit before the delay.
            f'tc qdisc add dev "$IFACE" parent 1:{mid} handle {mid}: '
            f'netem limit {limit} delay {delay:.1f}ms{jit}',
            f'tc filter add dev "$IFACE" protocol ip parent 1:0 prio 1 u32 '
            f'ht 2:{bucket}: match ip dst {h.private_ip}/32 flowid 1:{mid}',
        ]
    lines.append(f'echo "tc: {len(hosts) - 1} peer class(es) on $IFACE, '
                 f'hashed, netem limit {limit} pkts"')
    return "\n".join(lines) + "\n"


def verify_pairs(cfg: RunConfig, hosts: list[Host], count: int = 4) -> list[tuple[int, int]]:
    """Return pairs spanning the configured RTT distribution."""
    if len(hosts) < 2:
        return []
    idx = [h.index for h in hosts]
    candidates = [(i, j) for i in idx for j in idx if i < j]
    if not candidates:
        return []
    # Include both extremes and evenly spaced intermediate pairs.
    candidates.sort(key=lambda p: one_way_ms(cfg, p[0], p[1]))
    if len(candidates) <= count:
        return candidates
    step = (len(candidates) - 1) / (count - 1) if count > 1 else 0
    picked = {int(round(k * step)) for k in range(count)}
    return [candidates[k] for k in sorted(picked)]


_PING_RTT = r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/"


def verify_netem(ssh: Ssh, cfg: RunConfig, hosts: list[Host],
                 tolerance_pct: float = 25.0, floor_ms: float = 3.0) -> None:
    """Verify sampled RTTs and report netem queue drops."""
    pairs = verify_pairs(cfg, hosts)
    if not pairs:
        return
    by_index = {h.index: h for h in hosts}
    bad: list[str] = []
    for i, j in pairs:
        expected = one_way_ms(cfg, i, j) * 2
        out = ssh.run(by_index[i],
                      f"ping -c 5 -q {by_index[j].private_ip} | tail -2", check=False)
        m = re.search(_PING_RTT, out or "")
        if not m:
            bad.append(f"node{i}->node{j}: no RTT in ping output ({(out or '').strip()!r})")
            continue
        measured = float(m.group(1))
        low = expected * (1 - tolerance_pct / 100) - floor_ms
        print(f"wan: node{i}->node{j} expected ~{expected:.1f} ms, "
              f"measured {measured:.1f} ms", flush=True)
        if measured < low:
            bad.append(
                f"node{i}->node{j}: measured {measured:.1f} ms but expected "
                f"~{expected:.1f} ms -- shaping is missing or mis-filtered for this pair")
        elif measured > expected * (1 + tolerance_pct / 100) + floor_ms:
            print(f"wan: WARNING node{i}->node{j} is {measured / max(expected, 0.01):.2f}x "
                  f"the expected RTT (congestion, or jitter set too high)", flush=True)
    if bad:
        raise RuntimeError(
            "tc netem verification failed on "
            f"{len(bad)}/{len(pairs)} sampled pair(s): " + "; ".join(bad))
    report_netem_drops(ssh, hosts)


def parse_netem_drops(text: str) -> tuple[int, int, int]:
    """Parse ``(drops, qdiscs, counters)`` from ``tc -s qdisc show``."""
    total = qdiscs = counters = 0
    in_netem = False
    for raw_line in text.splitlines():
        line = raw_line.lstrip()
        if line.startswith("qdisc "):
            in_netem = re.match(r"qdisc\s+netem\b", line) is not None
            if in_netem:
                qdiscs += 1
        if not in_netem:
            continue
        match = re.search(r"\bdropped\s+(\d+)\b", line)
        if match:
            total += int(match.group(1))
            counters += 1
            in_netem = False
    return total, qdiscs, counters


def report_netem_drops(ssh: Ssh, hosts: list[Host]) -> int | None:
    """Report total netem drops; return None when counters are incomplete."""
    try:
        outs = ssh.fanout(hosts, lambda h: "tc -s qdisc show 2>/dev/null")
        expected_per_host = max(0, len(hosts) - 1)
        total = parsed_qdiscs = 0
        incomplete_hosts = 0
        for out in outs:
            drops, qdiscs, counters = parse_netem_drops(out or "")
            total += drops
            parsed_qdiscs += qdiscs
            if qdiscs != expected_per_host or counters != qdiscs:
                incomplete_hosts += 1
        if len(outs) != len(hosts) or incomplete_hosts:
            print(
                "wan: WARNING netem drop counters incomplete: received output from "
                f"{len(outs)}/{len(hosts)} host(s), {incomplete_hosts} output(s) invalid; "
                f"parsed {parsed_qdiscs} qdisc(s), "
                f"expected {expected_per_host} per host, so the fleet drop total is UNKNOWN",
                flush=True,
            )
            return None
        note = "" if total == 0 else (
            "  <-- NON-ZERO: netem dropped packets past its queue limit; these look like "
            "protocol packet loss. Raise wan.netem_limit_pkts")
        print(f"wan: netem dropped packets across the fleet: {total}{note}", flush=True)
        return total
    except Exception as exc:  # noqa: BLE001 -- observability only
        print(f"wan: could not read netem drop counters: {exc}", flush=True)
        return None


def apply_wan(ssh: Ssh, cfg: RunConfig, hosts: list[Host]) -> None:
    if cfg.wan.mode == "mimic":
        # Mimic mode is implemented by each protocol binary.
        print("wan: mimic mode -- protocol AWS RTT table enabled, no tc applied")
        return
    def apply(h: Host) -> str:
        return f"sudo bash -lc {_shq(tc_script(cfg, hosts, h.index))}"
    ssh.fanout(hosts, apply)
    verify_netem(ssh, cfg, hosts)


def clear_wan(ssh: Ssh, hosts: list[Host]) -> None:
    ssh.fanout(hosts, "sudo bash -lc 'tc qdisc del dev "
               "$(ip -o -4 route show to default | awk \"{print \\$5; exit}\") "
               "root 2>/dev/null || true'")


def _shq(s: str) -> str:
    import shlex
    return shlex.quote(s)
