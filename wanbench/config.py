"""Benchmark run configuration."""

from __future__ import annotations

import copy
import dataclasses
import ipaddress
import pathlib
from dataclasses import dataclass, field

try:
    import yaml
except ImportError as exc:  # pragma: no cover - surfaced at CLI startup
    raise SystemExit("wan-bench needs PyYAML: pip install -e .") from exc


@dataclass
class WanConfig:
    """Emulated wide-area network applied by tc netem on every instance."""

    # Square RTT matrix in milliseconds. Node i uses region i % matrix size.
    rtt_csv: str = "latency/aws-10region-rtt.csv"
    jitter_ms: float = 0.0
    # Apply half the RTT to each direction.
    halve_rtt: bool = True
    # netem shapes host traffic; mimic enables the protocol's internal delay table.
    mode: str = "netem"
    # Per-peer netem queue depth in packets.
    netem_limit_pkts: int = 100_000


@dataclass
class FaultConfig:
    """A fault to inject during a `run`. `kind` selects the mechanism."""

    kind: str = "none"  # none | crash | ring | split | blip
    at_s: int = 40  # seconds after load start to inject
    for_s: int = 15  # duration; crash is permanent regardless
    nodes: list[int] = field(default_factory=list)  # crash: which validators
    pct: int = 10  # ring: percent of each node's out-edges to cut
    group_a: list[int] = field(default_factory=list)  # split: side A
    group_b: list[int] = field(default_factory=list)  # split: side B
    mode: str = "cut"  # blip/split: cut (tcp-reset) | drop (silent pause)


@dataclass
class RunConfig:
    # --- identity / cloud ---
    run_id: str = "run1"
    region: str = "eu-west-1"
    # The fleet always uses one AZ. None selects the first AZ with enough capacity.
    az: str | None = None
    profile: str | None = None  # AWS profile; None = default chain
    # Set a type explicitly, or use "" to select from the limits below.
    instance_type: str = "c5d.2xlarge"
    # Auto-selection constraints.
    min_vcpu: int = 8
    min_mem_gib: int = 16
    min_local_nvme: bool = True
    instance_families: list[str] = field(default_factory=lambda: ["c", "m"])
    max_type_candidates: int = 6
    ami: str | None = None  # None = latest Ubuntu 22.04 LTS (resolved via SSM)
    key_name: str = ""  # EC2 keypair name registered in `region`
    ssh_key_path: str = "~/.ssh/id_rsa"  # matching private key on this host
    ssh_user: str = "ubuntu"
    # None allows this host's public IP.
    ssh_open_cidr: str | None = None
    spot: bool = False  # request Spot instances (cheaper, interruptible)
    spot_max_price: str | None = None  # None = on-demand price cap (safe default)
    disk_gb: int = 50
    # Mount local instance storage at /opt/wanbench when available.
    use_instance_store: bool = True
    # Retaining monitoring is opt-in; normal teardown releases every resource.
    keep_monitoring_on_down: bool = False
    # Native dashboard JSON files to provision with the generic dashboard.
    dashboards: list[str] = field(default_factory=list)

    # --- topology ---
    # Vantage-family protocols share one image; Starfish uses a separate adapter.
    protocol: str = "vantage"
    nodes: int = 10
    # Registry reference or local image name for build-on-control.
    image: str = ""
    image_source: str = "registry"  # registry | build-on-control
    build_repo: str = ""  # build-on-control: local path to the protocol repo
    build_dockerfile: str = ""  # relative Dockerfile path within build_repo

    # --- workload ---
    rate: int = 200  # aggregate input tx/s across all nodes
    tx_size: int = 512
    # random | all_zero; legacy all-zero is normalized during validation.
    tx_mode: str = "random"
    duration_s: int = 120
    # Extra entrypoint arguments, used by Starfish consensus selection.
    protocol_flags: list[str] = field(default_factory=list)
    # Broadcast Autobahn consensus votes instead of sending them to the leader.
    all_to_all: bool = False
    # Carry availability claims on AGB echoes instead of dedicated messages.
    echo_avail_claims: bool = True
    # Vantage sequence checkpoint recovery.
    sequence_checkpoints: bool = True
    sequence_install_enabled: bool = True
    sequence_checkpoint_interval_views: int = 20
    # Minimum cursor gap before checkpoint transfer.
    sequence_sync_min_gap_views: int = 50
    sequence_sync_chunk_outcomes: int = 256
    # Maximum outcome references per transfer chunk.
    sequence_sync_chunk_outcome_items: int = 1_600
    delta_ms: int = 200  # one-way delay bound; keep ~2x the WAN table's max one-way
    # Maximum wait before sealing a header.
    max_header_delay_ms: int = 100

    # --- metrics-active window ---
    # Delay load until deployment and committee formation should be complete.
    spam_initial_delay_ms: int = 10_000
    # Scale the delay with committee size to cover launch fan-out.
    spam_delay_per_node_ms: int = 200
    # Submit before measurement to exclude the initial load step.
    spam_lead_ms: int = 25_000
    # Computed by deploy from one timestamp.
    client_activate_at_ms: int | None = None
    metrics_active_at_ms: int | None = None

    # --- orphan protection ---
    # Instances terminate if the orchestrator stops renewing this deadline. 0 disables it.
    deadman_minutes: int = 60

    # --- network + faults ---
    wan: WanConfig = field(default_factory=WanConfig)
    fault: FaultConfig = field(default_factory=FaultConfig)

    # --- metrics ---
    metrics_port: int = 6003  # vantage primary metrics; starfish derives one per node
    # None allows this host's public IP; "" keeps the port closed.
    grafana_open_cidr: str | None = None
    # Short intervals increase validator scrape overhead.
    prometheus_scrape_interval_s: int = 30

    @property
    def tags(self) -> dict[str, str]:
        return {"Project": "wan-bench", "Run": self.run_id, "Protocol": self.protocol}

    def spam_delay_budget_ms(self) -> int:
        """Return the deployment delay before clients submit."""
        return self.spam_initial_delay_ms + self.spam_delay_per_node_ms * self.nodes

    @classmethod
    def from_dict(cls, data: dict) -> "RunConfig":
        raw = copy.deepcopy(data)
        wan = WanConfig(**(raw.pop("wan", {}) or {}))
        fault = FaultConfig(**(raw.pop("fault", {}) or {}))
        cfg = cls(**raw, wan=wan, fault=fault)
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str) -> "RunConfig":
        raw = yaml.safe_load(pathlib.Path(path).read_text()) or {}
        return cls.from_dict(raw)

    def validate(self) -> None:
        if not self.key_name:
            raise ValueError("config: key_name (EC2 keypair) is required")
        if not self.image:
            raise ValueError("config: image (GHCR node image) is required")
        if self.protocol not in ("vantage", "autobahn-seamless",
                                 "autobahn-optimistic", "simple-it",
                                 "simple-it-bracha", "starfish"):
            raise ValueError(f"config: unknown protocol {self.protocol!r}")
        if self.nodes < 1:
            raise ValueError("config: nodes must be >= 1")
        if self.rate < self.nodes or self.rate % self.nodes:
            raise ValueError(
                "config: rate must be >= nodes and divisible by nodes so every "
                "validator submits the same positive integer load")
        if self.tx_size < 1:
            raise ValueError("config: tx_size must be >= 1")
        self.tx_mode = self.tx_mode.replace("-", "_")  # legacy "all-zero" alias
        if self.tx_mode not in ("random", "all_zero"):
            raise ValueError(f"config: unknown tx_mode {self.tx_mode!r}")
        if self.duration_s < 1:
            raise ValueError("config: duration_s must be >= 1")
        if self.prometheus_scrape_interval_s < 1:
            raise ValueError("config: prometheus_scrape_interval_s must be >= 1")
        self._validate_ipv4_cidr("ssh_open_cidr", self.ssh_open_cidr)
        self._validate_ipv4_cidr(
            "grafana_open_cidr", self.grafana_open_cidr, allow_empty=True)
        if self.sequence_checkpoint_interval_views < 1:
            raise ValueError("config: sequence_checkpoint_interval_views must be >= 1")
        if self.sequence_sync_min_gap_views < 0:
            raise ValueError("config: sequence_sync_min_gap_views must be >= 0")
        if self.sequence_sync_chunk_outcomes < 1:
            raise ValueError("config: sequence_sync_chunk_outcomes must be >= 1")
        if self.sequence_sync_chunk_outcome_items < 1:
            raise ValueError("config: sequence_sync_chunk_outcome_items must be >= 1")
        if self.protocol == "vantage" and self.metrics_port != 6003:
            raise ValueError("config: vantage primary metrics_port is fixed at 6003")
        if self.protocol == "starfish" and self.nodes < 4:
            raise ValueError("config: starfish benchmark-genesis needs at least 4 nodes")
        if self.protocol == "starfish" and self.tx_size <= 16:
            raise ValueError("config: starfish tx_size must be > 16 bytes")
        if self.protocol == "starfish":
            self._validate_starfish_consensus()
        if self.image_source not in ("registry", "build-on-control"):
            raise ValueError(f"config: unknown image_source {self.image_source!r}")
        if self.image_source == "build-on-control" and not self.build_repo:
            raise ValueError("config: build-on-control needs build_repo")
        if self.wan.mode not in ("netem", "mimic"):
            raise ValueError(f"config: unknown wan.mode {self.wan.mode!r}")
        if self.wan.mode == "mimic":
            if self.wan.rtt_csv != WanConfig().rtt_csv:
                raise ValueError(
                    "config: mimic mode uses the protocol's built-in AWS RTT table; "
                    "custom wan.rtt_csv requires netem mode")
            if self.wan.jitter_ms or not self.wan.halve_rtt:
                raise ValueError(
                    "config: mimic mode supports fixed RTT/2 latency only; jitter and "
                    "halve_rtt=false require netem mode")
        f = self.fault
        if f.kind not in ("none", "crash", "ring", "split", "blip"):
            raise ValueError(f"config: unknown fault kind {f.kind!r}")
        if f.kind == "crash" and not f.nodes:
            raise ValueError("config: crash fault needs fault.nodes")
        if f.kind == "split" and not (f.group_a and f.group_b):
            raise ValueError("config: split fault needs group_a and group_b")
        if f.mode not in ("cut", "drop"):
            raise ValueError(f"config: fault.mode must be cut or drop, got {f.mode!r}")
        if not 1 <= f.pct <= 100:
            raise ValueError("config: fault.pct must be between 1 and 100")
        indices = [*f.nodes, *f.group_a, *f.group_b]
        invalid = sorted({i for i in indices if i < 0 or i >= self.nodes})
        if invalid:
            raise ValueError(f"config: fault node indices out of range: {invalid}")
        if set(f.group_a) & set(f.group_b):
            raise ValueError("config: split fault groups must be disjoint")

    @staticmethod
    def _validate_ipv4_cidr(name: str, value: str | None,
                            allow_empty: bool = False) -> None:
        if value is None or (allow_empty and value == ""):
            return
        if value == "":
            raise ValueError(f"config: {name} cannot be empty")
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise ValueError(f"config: invalid {name} {value!r}") from exc
        if network.version != 4:
            raise ValueError(f"config: {name} must be an IPv4 CIDR")

    # Values accepted by Starfish's --consensus option.
    STARFISH_CONSENSUS = frozenset({
        "mysticeti", "mysticeti-bls", "mysticeti-l",
        "cordial-miners",
        "starfish", "starfish-bls", "starfish-l", "starfish-speed", "starfish-s",
        "sparse-starfish-speed", "sparse-starfish", "ssfs",
        "sailfish++", "sailfish-pp",
        "bluestreak",
    })

    def _validate_starfish_consensus(self) -> None:
        """Reject missing or unsupported Starfish consensus names."""
        flags = self.protocol_flags
        if "--consensus" not in flags:
            raise ValueError(
                "config: starfish requires an explicit protocol_flags "
                "['--consensus', <name>]; omitting it silently runs plain starfish "
                f"(clap default), one of: {sorted(self.STARFISH_CONSENSUS)}")
        i = flags.index("--consensus")
        if i + 1 >= len(flags):
            raise ValueError("config: protocol_flags '--consensus' has no value")
        name = flags[i + 1]
        if name not in self.STARFISH_CONSENSUS:
            raise ValueError(
                f"config: unknown starfish consensus {name!r} -- the binary would "
                f"silently fall back to plain starfish. Known: "
                f"{sorted(self.STARFISH_CONSENSUS)}")

    def dumps(self) -> str:
        return yaml.safe_dump(dataclasses.asdict(self), sort_keys=False)
