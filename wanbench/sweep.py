"""Measure increasing loads on one fleet and stop after throughput collapse."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import subprocess
import time

from . import faults, monitoring, prepare, timing
from .collect import (PROGRESS_TIMEOUT_S, check_progress_quality, collect,
                      dump_failure_scrapes, wait_for_progress)
from .config import RunConfig
from .deploy import deploy, generate_keys
from .run import down, up
from .ssh import Host, Ssh

WARMUP_S = 30
WINDOW_S = 120
DROP_TOLERANCE_PCT = 5.0
_RETRYABLE_POINT_ERRORS = (
    RuntimeError,
    OSError,
    subprocess.SubprocessError,
)


def _reset_nodes(ssh: Ssh, hosts: list[Host]) -> None:
    """Stop validators and clear state from the previous point."""
    ssh.fanout(hosts, "sudo bash -lc 'docker rm -f wanbench-node 2>/dev/null || true; "
                      "rm -rf /opt/wanbench/store /opt/wanbench/storage-* "
                      "/opt/wanbench/logs'")


def sweep(cfg: RunConfig, rates: list[int], outdir: str,
          sweep_field: str = "rate",
          warmup_s: int = WARMUP_S, window_s: int = WINDOW_S,
          drop_tolerance_pct: float = DROP_TOLERANCE_PCT,
          stop_on_drop: bool = True,
          strict_through_rate: int | None = None,
          min_offered_throughput_pct: float | None = None,
          metric_labels: dict[str, str] | None = None,
          fleet: tuple[object, Ssh, list[Host], Host] | None = None) -> dict:
    if not rates:
        raise ValueError("sweep needs at least one rate")
    if sweep_field not in ("rate", "adversarial_rate"):
        raise ValueError("sweep field must be rate or adversarial_rate")
    if any(type(rate) is not int or
           (rate < 0 if sweep_field == "adversarial_rate" else rate <= 0)
           for rate in rates):
        qualifier = "non-negative" if sweep_field == "adversarial_rate" else "positive"
        raise ValueError(f"sweep rates must be {qualifier} integers, got {rates}")
    if rates != sorted(rates) or len(set(rates)) != len(rates):
        raise ValueError(f"sweep rates must be strictly increasing, got {rates}")
    publisher_count = (len(cfg.data_lane_drop_publishers)
                       if cfg.data_lane_drop_publishers
                       else cfg.data_lane_drop_staggered_senders)
    useful_nodes = cfg.nodes - publisher_count if cfg.correct_load_only else cfg.nodes
    if sweep_field == "rate":
        invalid_rates = [rate for rate in rates
                         if rate < useful_nodes or rate % useful_nodes]
        denominator = useful_nodes
    else:
        invalid_rates = [rate for rate in rates
                         if rate and (publisher_count == 0 or rate % publisher_count)]
        denominator = publisher_count
    if invalid_rates:
        raise ValueError(
            f"sweep {sweep_field} values must be exactly divisible by "
            f"{denominator} load-bearing nodes, got {invalid_rates}")
    if warmup_s < 0:
        raise ValueError(f"sweep warmup must be >= 0, got {warmup_s}")
    if window_s <= 0:
        raise ValueError(f"sweep window must be > 0, got {window_s}")
    if not 0 <= drop_tolerance_pct < 100:
        raise ValueError(
            f"sweep drop tolerance must be in [0, 100), got {drop_tolerance_pct}")
    if type(stop_on_drop) is not bool:
        raise ValueError("sweep stop_on_drop must be boolean")
    if (strict_through_rate is not None and
            (type(strict_through_rate) is not int or strict_through_rate <= 0)):
        raise ValueError("sweep strict_through_rate must be a positive integer or null")
    if (min_offered_throughput_pct is not None and
            (isinstance(min_offered_throughput_pct, bool) or
             not isinstance(min_offered_throughput_pct, (int, float)) or
             not 0 < min_offered_throughput_pct <= 100)):
        raise ValueError(
            "sweep min_offered_throughput_pct must be in (0, 100] or null")
    if cfg.fault.kind != "none":
        raise ValueError("sweep does not inject configured faults; set fault.kind to none")
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    result: dict = {"protocol": cfg.protocol, "nodes": cfg.nodes,
                    "warmup_s": warmup_s, "window_s": window_s,
                    "drop_tolerance_pct": drop_tolerance_pct,
                    "stop_on_drop": stop_on_drop,
                    "strict_through_rate": strict_through_rate,
                    "min_offered_throughput_pct": min_offered_throughput_pct,
                    "metric_labels": dict(metric_labels or {}),
                    "sweep_field": sweep_field,
                    "rates": rates, "points": [], "stopped_early": False,
                    "stop_reason": None, "status": "running", "error": None,
                    "effective_config": None}

    def checkpoint() -> None:
        path = out / "sweep.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(result, indent=2, allow_nan=False))
        temporary.replace(path)

    checkpoint()
    log = timing.StepLog("sweep")
    sweep_start = time.monotonic()
    result["timeline"] = {"steps": [], "points": [], "teardown_s": None,
                          "total_s": None}
    owns_fleet = fleet is None
    try:
        if owns_fleet:
            with log.step("up"):
                _aws, ssh, hosts, control = up(cfg)
        else:
            _aws, ssh, hosts, control = fleet
        result["effective_config"] = dataclasses.asdict(cfg)
        (out / "effective-config.yaml").write_text(cfg.dumps())
        checkpoint()
        result["timeline"]["steps"] = [[n, round(s)] for n, s in log.steps]
        try:
            # Vantage key generation is per node and reusable. Starfish owns its
            # committee-wide genesis inside deploy(), so the generic stub must not
            # be called here.
            with log.step("keygen"):
                pubkeys = None if cfg.protocol == "starfish" else generate_keys(
                    ssh, cfg, control, hosts)
            result["timeline"]["steps"] = [[n, round(s)] for n, s in log.steps]
            prev_tps: float | None = None
            for point_index, sweep_value in enumerate(rates, start=1):
                point_start = time.monotonic()
                setattr(cfg, sweep_field, sweep_value)
                strict_point = (
                    strict_through_rate is None or sweep_value <= strict_through_rate
                )
                print(f"sweep: point useful-offered={cfg.rate} tx/s; "
                      f"{sweep_field}={sweep_value} tx/s; "
                      f"validation={'strict' if strict_point else 'exploratory'}")
                # Renew orphan protection before each point.
                prepare.arm_deadman(ssh, hosts + [control], cfg.deadman_minutes,
                                    quiet=True)
                attempts_left = 2
                attempt = 0
                while True:
                    attempts_left -= 1
                    attempt += 1
                    point_stem = f"{sweep_field.replace('_', '-')}-{sweep_value}"
                    point_dir = (point_stem if attempt == 1
                                 else f"{point_stem}-attempt{attempt}")
                    captured_failure = False
                    try:
                        _reset_nodes(ssh, hosts)
                        labels = {
                            "wanbench_run": cfg.run_id,
                            "wanbench_protocol": cfg.protocol,
                            "wanbench_nodes": str(cfg.nodes),
                            "wanbench_rate": str(cfg.rate),
                            "wanbench_adversarial_rate": str(cfg.adversarial_rate),
                            **(metric_labels or {}),
                        }
                        targets = monitoring.validator_targets(cfg, hosts, labels)
                        try:
                            monitoring.configure_targets(ssh, cfg, control, targets)
                        except Exception as exc:  # noqa: BLE001 -- metrics are optional
                            print(f"sweep: WARNING monitoring labels unchanged ({exc})",
                                  flush=True)
                        deploy_start = time.monotonic()
                        deploy(ssh, cfg, control, hosts, pubkeys=pubkeys,
                               reuse_genesis=(cfg.protocol == "starfish" and
                                              bool(result["points"])))

                        progress_timeout = (
                            (cfg.spam_delay_budget_ms() + cfg.spam_lead_ms)
                            // 1000 + PROGRESS_TIMEOUT_S
                        )
                        # Retry the full committee; partial restarts do not rejoin safely.
                        stalled = wait_for_progress(ssh, cfg, control, hosts,
                                                    timeout_s=progress_timeout)
                        if stalled:
                            dump_failure_scrapes(ssh, cfg, control, hosts,
                                                 str(out / point_dir), "barrier")
                            captured_failure = True
                            raise RuntimeError(
                                f"node(s) {[h.index for h in stalled]} were not "
                                f"committing after {progress_timeout}s at the progress "
                                f"barrier (not relaunched individually by design)")
                        # Startup timing remains strict at every load.
                        try:
                            check_progress_quality(
                                ssh, cfg, control, hosts,
                                enforce_rate_and_lag=strict_point,
                            )
                        except RuntimeError:
                            dump_failure_scrapes(ssh, cfg, control, hosts,
                                                 str(out / point_dir), "quality")
                            captured_failure = True
                            raise
                        deploy_s = timing.since(deploy_start)
                        measure_start = time.monotonic()
                        summary = collect(
                            ssh, cfg, control, hosts, str(out / point_dir),
                            baseline_at=warmup_s, final_at=warmup_s + window_s,
                            strict=strict_point)
                        break
                    except _RETRYABLE_POINT_ERRORS as exc:
                        if not captured_failure:
                            dump_failure_scrapes(ssh, cfg, control, hosts,
                                                 str(out / point_dir), "failure")
                        if attempts_left <= 0:
                            raise
                        print(f"sweep: point {sweep_field}={sweep_value:,} failed "
                              f"({exc}); retrying "
                              f"once with a full node reset", flush=True)
                measure_s = timing.since(measure_start)
                result["timeline"]["points"].append(
                    {sweep_field: sweep_value, "rate": cfg.rate,
                     "adversarial_rate": cfg.adversarial_rate,
                     "deploy_s": deploy_s, "measure_s": measure_s})
                summary["strict_validation"] = strict_point
                summary["sweep_field"] = sweep_field
                summary["sweep_value"] = sweep_value
                summary["rate"] = cfg.rate
                summary["adversarial_rate"] = cfg.adversarial_rate
                result["points"].append(summary)
                checkpoint()
                tps = summary["tps_median"]
                print(
                    f"sweep: point {point_index}/{len(rates)} completed in "
                    f"{timing.since(point_start)}s; attempts={attempt}; "
                    f"useful-offered={cfg.rate:,}, {sweep_field}={sweep_value:,}, "
                    f"committed={tps:,.1f} tx/s",
                    flush=True,
                )
                if tps <= 0:
                    result["stopped_early"] = True
                    result["stop_reason"] = (
                        f"no committed progress at {sweep_field}={sweep_value} tx/s")
                    checkpoint()
                    print(f"sweep: EARLY STOP -- {result['stop_reason']}")
                    break
                below_offered = (
                    min_offered_throughput_pct is not None and
                    tps < cfg.rate * min_offered_throughput_pct / 100
                )
                if below_offered:
                    result["stopped_early"] = True
                    result["stop_reason"] = (
                        f"committed {tps:.1f} tx/s is below "
                        f"{min_offered_throughput_pct:g}% of the offered "
                        f"{cfg.rate} useful tx/s; overloaded")
                    checkpoint()
                    print(f"sweep: EARLY STOP -- {result['stop_reason']}")
                    break
                material_drop = (prev_tps is not None and
                                 tps < prev_tps * (1 - drop_tolerance_pct / 100))
                if stop_on_drop and material_drop:
                    result["stopped_early"] = True
                    result["stop_reason"] = (
                        f"committed TPS fell {prev_tps:.1f} -> {tps:.1f} "
                        f"(more than {drop_tolerance_pct:g}%) when {sweep_field} rose "
                        f"to {sweep_value} tx/s; past saturation")
                    checkpoint()
                    print(f"sweep: EARLY STOP -- {result['stop_reason']}")
                    break
                prev_tps = tps
        finally:
            if owns_fleet:
                teardown_start = time.monotonic()
                try:
                    try:
                        faults.clear(ssh, hosts)
                        prepare.clear_wan(ssh, hosts)
                    finally:
                        down(cfg)
                finally:
                    result["timeline"]["teardown_s"] = timing.since(teardown_start)
            else:
                result["timeline"]["teardown_s"] = 0
    except BaseException as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["timeline"]["total_s"] = timing.since(sweep_start)
        checkpoint()
        raise
    else:
        result["status"] = "completed"
        result["timeline"]["total_s"] = timing.since(sweep_start)
        checkpoint()

    print(f"sweep: {len(result['points'])}/{len(rates)} points -> {out / 'sweep.json'}")
    for p in result["points"]:
        print(f"  useful={p['rate']:>7} adversarial={p.get('adversarial_rate', 0):>7} "
              f"committed={p['tps_median']:>9.1f} tx/s "
              f"p50_since_start={p['ordering_p50_ms_since_start']}ms "
              f"p90_since_start={p['ordering_p90_ms_since_start']}ms "
              f"cpu={p['cpu_pct_median']}%")
    print(log.summary())
    for pt in result["timeline"]["points"]:
        print(f"  {sweep_field}={pt[sweep_field]:>7} deploy={pt['deploy_s']:>4}s "
              f"measure={pt['measure_s']:>4}s")
    print(f"sweep: teardown {result['timeline']['teardown_s']}s; "
          f"total {result['timeline']['total_s']}s")
    return result
