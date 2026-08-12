"""Orchestrate benchmark setup, execution, collection, and teardown."""

from __future__ import annotations

import json
import pathlib
import threading
import time

from . import faults, images, monitoring, prepare, timeseries, timing
from .aws import Aws
from .collect import collect
from .config import RunConfig
from .deploy import deploy
from .ssh import Ssh


def up(cfg: RunConfig) -> tuple[Aws, Ssh, list, object]:
    aws = Aws(cfg)
    ssh = Ssh(cfg.ssh_key_path, cfg.ssh_user)
    log = timing.StepLog("up")
    try:
        # Resolve external ingress before creating resources.
        if cfg.ssh_open_cidr is None or cfg.grafana_open_cidr is None:
            public_cidr = monitoring._my_ip()
            if cfg.ssh_open_cidr is None:
                cfg.ssh_open_cidr = public_cidr
            if cfg.grafana_open_cidr is None:
                cfg.grafana_open_cidr = public_cidr
        if cfg.image_source == "registry":
            # Pin one immutable image before provisioning.
            pinned, digest = images.pin_to_digest(cfg.image)
            if pinned != cfg.image:
                print(f"up: image manifest verified: {cfg.image}")
                print(f"up: pinned to immutable digest: {digest}")
                cfg.image = pinned
            else:
                print(f"up: image manifest verified: {cfg.image}")
        print(f"up: provisioning {cfg.nodes}+1 {cfg.instance_type} in {cfg.region}")
        with log.step("provision"):
            hosts = aws.provision()
            control = aws.control_host()
            allh = hosts + [control]
            info = aws.fleet_info()
            if isinstance(info, dict):
                cfg.az = info["az"]
                cfg.instance_type = info["instance_type"]
                cfg.ami = info["ami"]
        with log.step("ssh ready"):
            ssh.wait_ready(allh)
        # Arm host-side termination before other preparation steps.
        prepare.arm_deadman(ssh, allh, cfg.deadman_minutes)
        print("up: installing docker")
        with log.step("docker install"):
            prepare.install_docker(ssh, allh)
            prepare.tune_control_sshd(ssh, control)
        if cfg.use_instance_store:
            print("up: mounting local NVMe for the store")
            with log.step("NVMe mount"):
                prepare.mount_instance_store(ssh, allh)
        print(f"up: preparing image (source={cfg.image_source})")
        with log.step("image prepare+pull"):
            ref = images.ensure_image(ssh, cfg, control, hosts)
            prepare.pull_image(ssh, hosts, ref)
            cfg.image = ref
        print(f"up: applying WAN ({cfg.wan.mode}, private IPs)")
        with log.step("WAN"):
            prepare.apply_wan(ssh, cfg, hosts)
        targets = monitoring.validator_targets(cfg, hosts)
        with log.step("monitoring"):
            try:
                url = monitoring.start(aws, ssh, cfg, control, hosts, targets, cfg.dashboards)
                print(f"up: Grafana {url}")
            except Exception as e:
                # Collection can continue without the browser dashboard.
                print(f"up: WARNING monitoring unavailable ({e}); fleet is up and "
                      f"measurable; deploy/collect proceed", flush=True)
        print(log.summary())
        return aws, ssh, hosts, control
    except BaseException:
        # Setup failures occur before callers can enter their teardown blocks.
        try:
            ids = aws.terminate(keep_control=False)
            aws._wait_terminated(role=None)
            groups = aws.delete_security_group()
            print(f"up: failed; terminated {len(ids)} instance(s): {ids}")
            print(f"up: failed; deleted {len(groups)} security group(s): {groups}")
        except Exception as cleanup_error:
            print(f"up: failed; teardown also failed: {cleanup_error}")
        raise


def down(cfg: RunConfig, keep_monitoring: bool | None = None) -> None:
    """Terminate this run and delete its security group unless monitoring is kept."""
    aws = Aws(cfg)
    keep = cfg.keep_monitoring_on_down if keep_monitoring is None else keep_monitoring
    # Resolve the retained host before termination changes fleet state.
    control = None
    if keep:
        try:
            control = aws.control_host()
        except RuntimeError:
            control = None
    ids = aws.terminate(keep_control=keep)
    print(f"down: terminated {len(ids)} instance(s): {ids}")
    if keep:
        if control is not None:
            print(f"down: monitoring host KEPT at {control.public_ip} "
                  f"(grafana :3000, prometheus :9090) -- pull results from it, then "
                  f"`wanbench down --no-keep-monitoring` or `nuke` to release it")
        else:
            print("down: no monitoring host was running")
    else:
        aws._wait_terminated(role=None)
        groups = aws.delete_security_group()
        print(f"down: deleted {len(groups)} security group(s): {groups}")


def run(cfg: RunConfig, outdir: str) -> dict:
    aws, ssh, hosts, control = up(cfg)
    try:
        deploy_start = time.monotonic()
        deploy(ssh, cfg, control, hosts)
        deploy_s = timing.since(deploy_start)
        # Apply timed faults without delaying collection.
        fault_thread = None
        if cfg.fault.kind != "none":
            fault_thread = _schedule_fault(ssh, cfg, hosts, outdir)
        collect_start = time.monotonic()
        summary = collect(ssh, cfg, control, hosts, outdir)
        collect_s = timing.since(collect_start)
        if fault_thread is not None:
            fault_thread.join(timeout=30)
            try:
                timeseries.dump(ssh, cfg, control, outdir)
            except Exception as exc:  # noqa: BLE001 -- the summary must survive
                print(f"timeseries: WARNING dump failed: {exc}", flush=True)
        print(f"run: deploy took {deploy_s}s")
        print(f"run: collect took {collect_s}s")
        return summary
    finally:
        # Always terminate, even if fault cleanup fails.
        try:
            faults.clear(ssh, hosts)
            prepare.clear_wan(ssh, hosts)
        finally:
            down(cfg)


def _fault_delay_s(anchor_ms: float | None, at_s: float, now_s: float) -> float:
    """Seconds to sleep so the fault fires at_s after the metrics window opens."""
    anchor_s = now_s if anchor_ms is None else anchor_ms / 1000.0
    return max(0.0, anchor_s + at_s - now_s)


def _schedule_fault(ssh: Ssh, cfg: RunConfig, hosts: list,
                    outdir: str) -> threading.Thread:
    def worker():
        timeline = {
            "kind": cfg.fault.kind,
            "nodes": sorted(cfg.fault.nodes),
            "at_s": cfg.fault.at_s,
            "for_s": cfg.fault.for_s,
            "anchor_ms": cfg.metrics_active_at_ms,
            "down_ms": None,
            "up_ms": None,
            "error": None,
        }
        try:
            time.sleep(_fault_delay_s(cfg.metrics_active_at_ms, cfg.fault.at_s,
                                      time.time()))
            faults.apply_from_config(ssh, cfg, hosts)
            timeline["down_ms"] = int(time.time() * 1000)
            if cfg.fault.for_s > 0:
                time.sleep(cfg.fault.for_s)
                if cfg.fault.kind in ("split", "blip"):
                    faults.clear(ssh, hosts)
                    timeline["up_ms"] = int(time.time() * 1000)
                elif cfg.fault.kind == "crash":
                    faults.restart(ssh, hosts, cfg.fault.nodes)
                    timeline["up_ms"] = int(time.time() * 1000)
        except Exception as exc:  # noqa: BLE001 -- a dead fault thread must be loud
            timeline["error"] = str(exc)
            print(f"fault: FAILED: {exc}", flush=True)
        finally:
            _write_fault_timeline(outdir, timeline)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def _write_fault_timeline(outdir: str, timeline: dict) -> None:
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "fault-timeline.json").write_text(json.dumps(timeline, indent=2))
    print(f"fault: timeline written ({timeline['kind']}, "
          f"down_ms={timeline['down_ms']}, up_ms={timeline['up_ms']})", flush=True)
