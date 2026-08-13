"""Protocol-specific committee, parameter, and launch adapters."""

from __future__ import annotations

import abc
import base64
import json

from .config import RunConfig
from .ssh import Host

# Vantage host-network port layout.
VANTAGE_PORTS = {
    "consensus_to_consensus": 6000,
    "primary_to_primary": 6001,
    "worker_to_primary": 6002,
    "primary_metrics": 6003,
    "primary_to_worker": 6004,
    "transactions": 6005,
    "worker_to_worker": 6006,
    "worker_metrics": 6007,
}
CONSENSUS_PORT = VANTAGE_PORTS["primary_to_primary"]
WORKER_PORT = VANTAGE_PORTS["worker_to_worker"]
TX_PORT = VANTAGE_PORTS["transactions"]


class ProtocolAdapter(abc.ABC):
    def __init__(self, cfg: RunConfig):
        self.cfg = cfg

    @abc.abstractmethod
    def keygen_cmd(self, index: int) -> str:
        """`docker run <image> ...` whose stdout is node `index`'s key JSON."""

    @abc.abstractmethod
    def committee(self, hosts: list[Host], pubkeys: list[dict]) -> dict:
        """Committee document from private IPs + per-node pubkeys."""

    @abc.abstractmethod
    def run_cmd(self, host: Host, hosts: list[Host]) -> str:
        """Return the container command for one node."""

    def parameters(self, pubkeys: list[dict] | None = None) -> dict | None:
        """Return parameters.json content, or None for generated parameters."""
        return None

    def ports(self) -> dict[str, int]:
        return {"consensus": CONSENSUS_PORT, "worker": WORKER_PORT,
                "tx": TX_PORT, "metrics": self.cfg.metrics_port}


def _docker_prefix(cfg: RunConfig, idx: int, entrypoint: str, env_extra: str = "") -> str:
    publishers = set(getattr(cfg, "_data_lane_drop_runtime_publishers", []))
    if (cfg.correct_load_only or cfg.adversarial_rate) and not publishers:
        raise ValueError(
            "leader-relay workload requires resolved withholding publisher indices")
    useful_nodes = [
        node for node in range(cfg.nodes)
        if not cfg.correct_load_only or node not in publishers
    ]
    if cfg.rate < len(useful_nodes) or cfg.rate % len(useful_nodes):
        raise ValueError(
            f"aggregate useful rate {cfg.rate} must be >= and divisible by "
            f"{len(useful_nodes)} load-bearing validators")
    rate_each = cfg.rate // len(useful_nodes) if idx in useful_nodes else 0
    if cfg.adversarial_rate and cfg.adversarial_rate % len(publishers):
        raise ValueError(
            f"aggregate adversarial rate {cfg.adversarial_rate} must be divisible by "
            f"{len(publishers)} withholding publishers")
    adversarial_rate_each = (
        cfg.adversarial_rate // len(publishers)
        if idx in publishers and cfg.adversarial_rate
        else 0
    )
    ep = f"--entrypoint {entrypoint} " if entrypoint else ""
    # n=100 exceeds Docker's default 1024-file limit with full-mesh connections.
    return (f"docker run -d --name wanbench-node --restart no --network host "
            f"--ulimit nofile=65536:65536 "
            f"--cap-add NET_ADMIN -v /opt/wanbench:/wanbench "
            f"-e NODE_INDEX={idx} -e N_NODES={cfg.nodes} "
            f"-e RATE={rate_each} -e ADVERSARIAL_RATE={adversarial_rate_each} "
            f"-e TX_SIZE={cfg.tx_size} "
            f"-e METRICS_PORT={cfg.metrics_port} {env_extra} {ep}{cfg.image}")


class Vantage(ProtocolAdapter):
    def keygen_cmd(self, index: int) -> str:
        return (f"docker run --rm --entrypoint node {self.cfg.image} "
                f"generate_keys --filename /dev/stdout")

    def committee(self, hosts: list[Host], pubkeys: list[dict]) -> dict:
        # config::Authority requires every address below.
        p = VANTAGE_PORTS
        authorities = {}
        for h, pk in zip(hosts, pubkeys):
            ip = h.private_ip
            authorities[pk["name"]] = {
                "stake": 1,
                "consensus": {
                    "consensus_to_consensus": f"{ip}:{p['consensus_to_consensus']}"},
                "primary": {"primary_to_primary": f"{ip}:{p['primary_to_primary']}",
                            "worker_to_primary": f"{ip}:{p['worker_to_primary']}",
                            "metrics": f"{ip}:{p['primary_metrics']}"},
                "workers": {"0": {"primary_to_worker": f"{ip}:{p['primary_to_worker']}",
                                  "transactions": f"{ip}:{p['transactions']}",
                                  "worker_to_worker": f"{ip}:{p['worker_to_worker']}",
                                  "metrics": f"{ip}:{p['worker_metrics']}"}},
            }
        return {"authorities": authorities}

    def parameters(self, pubkeys: list[dict] | None = None) -> dict:
        # Complete config::Parameters document for the Vantage binary.
        parameters = {
            "timeout_delay": 1000,
            "header_size": 1000,
            "max_header_delay": self.cfg.max_header_delay_ms,
            "gc_depth": 50,
            "sync_retry_delay": 5000,
            "sync_retry_nodes": 3,
            "batch_size": 500_000,
            "max_batch_delay": 20,
            "use_optimistic_tips": True,
            "use_parallel_proposals": True,
            "k": 4,
            "use_fast_path": True,
            "fast_path_timeout": 500,
            "use_ride_share": False,
            "car_timeout": 2000,
            "all_to_all": self.cfg.all_to_all,
            "simulate_asynchrony": False,
            "asynchrony_start": 20_000,
            "asynchrony_duration": 10_000,
            "protocol": "vantage",
            "tx_mode": self.cfg.tx_mode,
            "max_block_payload": 16,
            "delta_ms": self.cfg.delta_ms,
            # Transactions submitted before this timestamp are excluded.
            "metrics_active_at_ms": self.cfg.metrics_active_at_ms,
            "vantage_gc_window_views": 200,
            "simpleit_gc_window_rounds": 50,
            "ack_watermarks": True,
            "ack_watermark_period_ms": 50,
            "digest_statements": True,
            "echo_avail_claims": self.cfg.echo_avail_claims,
            "vantage_compact_ids": self.cfg.vantage_compact_ids,
            "sequence_checkpoints": self.cfg.sequence_checkpoints,
            "sequence_install_enabled": self.cfg.sequence_install_enabled,
            "sequence_checkpoint_interval_views": (
                self.cfg.sequence_checkpoint_interval_views
            ),
            "sequence_sync_min_gap_views": self.cfg.sequence_sync_min_gap_views,
            "sequence_sync_chunk_outcomes": self.cfg.sequence_sync_chunk_outcomes,
            "sequence_sync_chunk_outcome_items": (
                self.cfg.sequence_sync_chunk_outcome_items
            ),
            # Zero disables internal latency when host netem is active.
            "mimic_latency_ms": None if self.cfg.wan.mode == "mimic" else 0,
            "batch_messages": True,
            "batch_max_bytes": 65_536,
            "batch_max_delay_ms": 5,
            "withhold_senders": 0,
            "withhold_publishers": [],
            "withhold_count": None,
            "withhold_stride": 1,
            "withhold_receivers": [],
            "withhold_repair": self.cfg.data_lane_drop_silent_repair,
            "withhold_headers": self.cfg.data_lane_drop_headers,
            "withhold_at_ms": None,
            "withhold_for_ms": 30_000,
            "resume_check_period_ms": 1000,
            "resume_backoff_ms": 4000,
            "resume_batch": 64,
            "reconnect_replay": True,
            "retry_backoff_max_ms": 2000,
        }
        publishers = self.cfg.data_lane_drop_publishers
        receivers = self.cfg.data_lane_drop_receivers
        if publishers:
            if pubkeys is None or len(pubkeys) != self.cfg.nodes:
                raise ValueError(
                    "data-lane drop parameters require one generated public key "
                    "per validator")
            try:
                parameters["withhold_publishers"] = [
                    pubkeys[index]["name"] for index in publishers
                ]
                parameters["withhold_receivers"] = [
                    pubkeys[index]["name"] for index in receivers
                ]
            except (IndexError, KeyError, TypeError) as exc:
                raise ValueError(
                    "data-lane drop could not map validator indices to public keys"
                ) from exc
            parameters["withhold_count"] = len(receivers)
        elif self.cfg.data_lane_drop_staggered_senders:
            if pubkeys is None or len(pubkeys) != self.cfg.nodes:
                raise ValueError(
                    "staggered data-lane drop parameters require one generated "
                    "public key per validator")
            try:
                committee_order = sorted(
                    (entry["name"] for entry in pubkeys),
                    key=base64.b64decode,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    "staggered data-lane drop could not order generated public keys"
                ) from exc
            parameters["withhold_publishers"] = [
                committee_order[
                    (offset * self.cfg.data_lane_drop_publisher_stride)
                    % self.cfg.nodes
                ]
                for offset in range(self.cfg.data_lane_drop_staggered_senders)
            ]
            parameters["withhold_count"] = self.cfg.data_lane_drop_staggered_width
            parameters["withhold_stride"] = self.cfg.data_lane_drop_staggered_stride
        selected = parameters["withhold_publishers"]
        if selected:
            if pubkeys is None:
                raise ValueError("data-lane publisher resolution requires generated keys")
            index_by_key = {
                entry["name"]: index for index, entry in enumerate(pubkeys)
            }
            try:
                self.cfg._data_lane_drop_runtime_publishers = [
                    index_by_key[key] for key in selected
                ]
            except KeyError as exc:
                raise ValueError(
                    "data-lane publisher key is absent from the generated committee") from exc
        else:
            self.cfg._data_lane_drop_runtime_publishers = []
        return parameters

    ENTRYPOINT = "/usr/local/bin/wanbench-entrypoint.sh"

    def run_cmd(self, host: Host, hosts: list[Host]) -> str:
        # Vantage accepts protocol settings through parameters.json, not argv.
        if self.cfg.protocol_flags:
            raise ValueError(
                f"vantage's `node run` rejects extra argv {self.cfg.protocol_flags}; "
                f"express the knob in the adapter's parameters() instead")
        own = f"{host.private_ip}:{TX_PORT}"
        peers = " ".join(f"{h.private_ip}:{TX_PORT}" for h in hosts)
        env = f"-e OWN_TX_ADDR={own} -e PEER_TX_ADDRS='{peers}' -e TX_MODE={self.cfg.tx_mode}"
        # Clients start before the node metrics gate to warm the transport.
        if self.cfg.client_activate_at_ms is not None:
            env += f" -e ACTIVATE_AT_MS={self.cfg.client_activate_at_ms}"
        return _docker_prefix(self.cfg, host.index, self.ENTRYPOINT, env)


# Vantage-binary variants differ only in their parameters.
class _VantageBinaryProtocol(Vantage):
    """A `node`-binary assembly that differs from vantage only in `parameters()`."""

    PROTOCOL: str = ""

    def parameters(self, pubkeys: list[dict] | None = None) -> dict:
        p = super().parameters(pubkeys)
        p["protocol"] = self.PROTOCOL
        return p


class AutobahnSeamless(_VantageBinaryProtocol):
    PROTOCOL = "autobahn-seamless"


class AutobahnOptimistic(_VantageBinaryProtocol):
    PROTOCOL = "autobahn-optimistic"


# Simple-IT uses (d_s + d_t) * Delta from Table 3 of arXiv:2606.14404.
class _SimpleIt(_VantageBinaryProtocol):
    RBC_DELAYS: tuple[int, int] = (0, 0)   # (d_s, d_t) from Table 3

    def parameters(self, pubkeys: list[dict] | None = None) -> dict:
        p = super().parameters(pubkeys)
        d_s, d_t = self.RBC_DELAYS
        p["timeout_delay"] = (d_s + d_t) * self.cfg.delta_ms
        return p


class SimpleIt(_SimpleIt):
    """Simple-IT over Opt-RBC with an 8*Delta timer."""

    PROTOCOL = "simple-it"
    RBC_DELAYS = (4, 4)


class SimpleItBracha(_SimpleIt):
    """Simple-IT over Bracha-RBC with a 5*Delta timer."""

    PROTOCOL = "simple-it-bracha"
    RBC_DELAYS = (3, 2)


class Starfish(ProtocolAdapter):
    def keygen_cmd(self, index: int) -> str:
        raise NotImplementedError("starfish uses committee-wide benchmark-genesis")

    def committee(self, hosts: list[Host], pubkeys: list[dict]) -> dict:
        raise NotImplementedError("starfish committee is produced by benchmark-genesis")

    def run_cmd(self, host: Host, hosts: list[Host]) -> str:
        flags = " ".join(self.cfg.protocol_flags)
        return (_docker_prefix(self.cfg, host.index, "starfish") +
                f" run --authority {host.index} "
                f"--committee-path /wanbench/committee.yaml "
                f"--public-config-path /wanbench/public-config.yaml "
                f"--private-config-path /wanbench/private-config-{host.index}.yaml "
                f"--parameters-path /wanbench/parameters.yaml {flags}")

    def genesis_cmd(self, private_ips: list[str]) -> str:
        ips = " ".join(private_ips)
        return (f"docker run --rm -v /opt/wanbench:/wanbench {self.cfg.image} "
                f"benchmark-genesis --ips {ips} --working-directory /wanbench "
                f"--node-parameters-path /wanbench/node-parameters.yaml")


def uses_vantage_ports(protocol: str) -> bool:
    """Return whether a protocol uses the Vantage port layout."""
    return protocol != "starfish"


def adapter_for(cfg: RunConfig) -> ProtocolAdapter:
    return {
        "vantage": Vantage,
        "autobahn-seamless": AutobahnSeamless,
        "autobahn-optimistic": AutobahnOptimistic,
        "simple-it": SimpleIt,
        "simple-it-bracha": SimpleItBracha,
        "starfish": Starfish,
    }[cfg.protocol](cfg)


def committee_json(cfg: RunConfig, hosts: list[Host], pubkeys: list[dict]) -> str:
    return json.dumps(adapter_for(cfg).committee(hosts, pubkeys), indent=2)
