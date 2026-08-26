"""Generate keys, distribute configuration, and launch validators."""

from __future__ import annotations

import concurrent.futures
import json
import pathlib
import re
import shlex
import tempfile
import time

from .config import RunConfig
from .protocols import KEY_DIR, adapter_for
from .ssh import Host, Ssh


CLIENT_ACTIVATION_MARGIN_MS = 5_000


def _parameters(cfg: RunConfig, pubkeys: list[dict] | None = None) -> dict:
    # Protocol adapters own their parameter schema.
    params = adapter_for(cfg).parameters(pubkeys)
    if params is None:
        raise ValueError(f"{cfg.protocol}: no parameters template (protocol mints "
                         f"its own; this deploy path should not be reached)")
    return params


def generate_keys(ssh: Ssh, cfg: RunConfig, control: Host, hosts: list[Host]) -> list[dict]:
    """Run keygen once per node on the control host; return pubkey dicts in order."""
    adapter = adapter_for(cfg)
    ssh.run(control, f"sudo mkdir -p {KEY_DIR} && "
                     f"sudo chown -R $(id -u) {KEY_DIR}")

    def one(h) -> dict:
        out = ssh.run(control, f"sudo {adapter.keygen_cmd(h.index)}", timeout=300)
        key = json.loads(out)
        # Store each private key only until it is copied to its validator.
        ssh.run(control, f"cat > {KEY_DIR}/key-{h.index}.json <<'EOF'\n{out}\nEOF")
        public = _public_only(key)
        # A post-quantum committee also publishes a consensus public key. The
        # key file carries only its private half and `_public_only` strips
        # that, so the public half has to come back from the binary. Reads the
        # file written just above.
        pubkey_cmd = adapter.consensus_pubkey_cmd(h.index)
        if pubkey_cmd is not None:
            printed = ssh.run(control, f"sudo {pubkey_cmd}", timeout=300).strip()
            if not printed:
                raise ValueError(
                    f"node {h.index}: no consensus public key for scheme "
                    f"{cfg.signature_scheme}")
            public["consensus_key"] = printed
        return public

    # Preserve host order and limit concurrent sessions on the control host.
    workers = min(16, max(1, len(hosts)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, hosts))


def _public_only(key: dict) -> dict:
    return {k: v for k, v in key.items() if "secret" not in k.lower() and "private" not in k.lower()} \
        or {k: v for k, v in key.items() if "public" in k.lower()}


def deploy(ssh: Ssh, cfg: RunConfig, control: Host, hosts: list[Host],
           pubkeys: list[dict] | None = None, reuse_genesis: bool = False) -> None:
    """Distribute configuration and relaunch every validator."""
    if cfg.protocol == "starfish":
        return _deploy_starfish(ssh, cfg, control, hosts, reuse_genesis)
    adapter = adapter_for(cfg)
    if pubkeys is None:
        pubkeys = generate_keys(ssh, cfg, control, hosts)

    committee = json.dumps(adapter.committee(hosts, pubkeys), indent=2)

    with tempfile.TemporaryDirectory() as tmp:
        cpath = pathlib.Path(tmp) / "committee.json"
        ppath = pathlib.Path(tmp) / "parameters.json"
        cpath.write_text(committee)

        # Fetch keys with bounded concurrency before the validator fan-out.
        def fetch_key(h: Host) -> None:
            keytext = ssh.run(control, f"cat {KEY_DIR}/key-{h.index}.json")
            (pathlib.Path(tmp) / f"key-{h.index}.json").write_text(keytext)

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(16, max(1, len(hosts)))) as pool:
            list(pool.map(fetch_key, hosts))

        def push(h: Host) -> None:
            ssh.run(h, "sudo mkdir -p /opt/wanbench && sudo chown $(id -u) /opt/wanbench")
            ssh.scp(h, str(cpath), "/opt/wanbench/committee.json")
            ssh.scp(h, str(pathlib.Path(tmp) / f"key-{h.index}.json"), "/opt/wanbench/key.json")

        ssh.parallel(hosts, push)

        budget = cfg.spam_delay_budget_ms()
        if budget > 0:
            anchor = int(time.time() * 1000)
            activation_delay = CLIENT_ACTIVATION_MARGIN_MS + budget
            cfg.client_activate_at_ms = anchor + activation_delay
            cfg.metrics_active_at_ms = cfg.client_activate_at_ms + cfg.spam_lead_ms
            print(
                f"deploy: clients submit in {activation_delay} ms "
                f"(node lead budget {budget} ms); metrics-active window opens "
                f"{cfg.spam_lead_ms} ms later at {cfg.metrics_active_at_ms} epoch ms"
            )
        else:
            cfg.client_activate_at_ms = None
            cfg.metrics_active_at_ms = None

        ppath.write_text(json.dumps(_parameters(cfg, pubkeys), indent=2))
        ssh.parallel(
            hosts,
            lambda h: ssh.scp(h, str(ppath), "/opt/wanbench/parameters.json"),
        )

    launch_nodes(ssh, cfg, hosts, hosts)


def launch_nodes(ssh: Ssh, cfg: RunConfig, targets: list[Host],
                 all_hosts: list[Host]) -> None:
    """Relaunch selected validators using the full committee configuration."""
    adapter = adapter_for(cfg)

    def launch(h: Host) -> str:
        inner = ("docker rm -f wanbench-node 2>/dev/null || true; "
                 + adapter.run_cmd(h, all_hosts))
        return "sudo bash -lc " + shlex.quote(inner)

    ssh.fanout(targets, launch, max_workers=len(targets))
    scope = ("" if len(targets) == len(all_hosts)
             else f" (of {len(all_hosts)}): {[h.index for h in targets]}")
    print(f"deploy: launched {len(targets)} {cfg.protocol} node(s){scope}", flush=True)


def _deploy_starfish(ssh: Ssh, cfg: RunConfig, control: Host, hosts: list[Host],
                     reuse_genesis: bool = False) -> None:
    """Generate and distribute Starfish configuration, then launch validators."""
    from .protocols import Starfish
    adapter = Starfish(cfg)
    ips = [h.private_ip for h in hosts]
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        if not reuse_genesis:
            # Clear the mount contents without removing the mountpoint.
            ssh.run(control, "sudo find /opt/wanbench -mindepth 1 -delete; "
                             "sudo mkdir -p /opt/wanbench && "
                             "sudo chown $(id -u) /opt/wanbench")
            node_params = root / "node-parameters.yaml"
            node_params.write_text(json.dumps(
                adapter.node_parameters(), indent=2))
            ssh.scp(control, str(node_params), "/opt/wanbench/node-parameters.yaml")
            ssh.run(control, f"sudo {adapter.genesis_cmd(ips)}", timeout=180)

        common = ("committee.yaml", "public-config.yaml")
        per_node = tuple(f"private-config-{h.index}.yaml" for h in hosts)
        # Fetch from the control host before validator fan-out.
        for name in (*common, *per_node):
            text = ssh.run(control, f"sudo cat /opt/wanbench/{name}")
            (root / name).write_text(text)

        # A binary without block authentication ignores the unknown key in
        # node-parameters.yaml, which would silently yield an Ed25519 fleet
        # under a post-quantum label. Genesis echoes the scheme it actually
        # used into public-config.yaml, so check it rather than trust it.
        if cfg.signature_scheme != "ed25519":
            published = (root / "public-config.yaml").read_text()
            if not re.search(rf"^\s*block_authentication:\s*{re.escape(cfg.signature_scheme)}\s*$",
                             published, re.MULTILINE):
                raise ValueError(
                    f"genesis did not apply signature_scheme "
                    f"{cfg.signature_scheme!r}: public-config.yaml does not "
                    f"name it. The image predates block authentication.")

        parameters = root / "parameters.yaml"
        parameters.write_text(json.dumps({
            "load": cfg.rate // cfg.nodes,
            "transaction_size": cfg.tx_size,
            "transaction_mode": cfg.tx_mode,
        }, indent=2))

        def push(h: Host) -> None:
            ssh.run(h, "sudo mkdir -p /opt/wanbench && "
                       "sudo chown $(id -u) /opt/wanbench")
            for name in (*common, f"private-config-{h.index}.yaml"):
                ssh.scp(h, str(root / name), f"/opt/wanbench/{name}")
            ssh.scp(h, str(parameters), "/opt/wanbench/parameters.yaml")

        ssh.parallel(hosts, push)

    launch_nodes(ssh, cfg, hosts, hosts)
