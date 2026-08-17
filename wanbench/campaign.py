"""Run a protocol matrix on one AWS fleet."""

from __future__ import annotations

import copy
import dataclasses
import datetime
import json
import pathlib
import re
import time
from dataclasses import dataclass

import yaml

from . import faults, images, monitoring, prepare, sweep as sweep_mod
from .aws import Aws
from .config import RunConfig
from .run import down, up
from .ssh import Host, Ssh


_VARIANT_FIELDS = frozenset({
    "protocol",
    "image",
    "image_source",
    "build_repo",
    "build_dockerfile",
    "metrics_port",
    "protocol_flags",
    "all_to_all",
    "channel_auth",
    "echo_avail_claims",
    "vantage_compact_ids",
    "dashboards",
})


@dataclass(frozen=True)
class CampaignVariant:
    name: str
    overrides: dict


@dataclass(frozen=True)
class CampaignConfig:
    name: str
    output: str
    sweep_field: str
    rates: list[int]
    warmup_s: int
    window_s: int
    drop_tolerance_pct: float
    stop_on_drop: bool
    strict_through_rate: int | None
    min_offered_throughput_pct: float | None
    committee_sizes: list[int]
    base: dict
    variants: list[CampaignVariant]

    @classmethod
    def load(cls, path: str) -> "CampaignConfig":
        raw = yaml.safe_load(pathlib.Path(path).read_text()) or {}
        if not isinstance(raw, dict):
            raise ValueError("campaign config must be a mapping")
        allowed = {
            "name", "output", "sweep_field", "rates", "warmup_s", "window_s",
            "drop_tolerance_pct", "stop_on_drop", "strict_through_rate",
            "min_offered_throughput_pct",
            "committee_sizes",
            "base", "variants",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"campaign: unknown fields {unknown}")

        name = raw.get("name", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise ValueError("campaign: name must contain lowercase letters, digits, or '-'")
        output = raw.get("output") or f"results/{name}"
        if not isinstance(output, str):
            raise ValueError("campaign: output must be a path string")
        base = raw.get("base", {})
        if base is None:
            base = {}
        if not isinstance(base, dict):
            raise ValueError("campaign: base must be a mapping")
        committee_sizes = raw.get("committee_sizes")
        if committee_sizes is None:
            committee_sizes = [base.get("nodes", 10)]
        elif "nodes" in base:
            raise ValueError(
                "campaign: base.nodes cannot be combined with committee_sizes")
        if (not isinstance(committee_sizes, list) or not committee_sizes or
                any(type(nodes) is not int or nodes <= 0
                    for nodes in committee_sizes)):
            raise ValueError(
                "campaign: committee_sizes must be positive integers")
        if committee_sizes != sorted(set(committee_sizes)):
            raise ValueError(
                "campaign: committee_sizes must be strictly increasing")
        items = raw.get("variants", [])
        if items is None:
            items = []
        if not isinstance(items, list):
            raise ValueError("campaign: variants must be a list")
        sweep_field = raw.get("sweep_field", "rate")
        if sweep_field not in ("rate", "adversarial_rate"):
            raise ValueError(
                "campaign: sweep_field must be 'rate' or 'adversarial_rate'")
        rates = raw.get("rates")
        if (not isinstance(rates, list) or not rates or
                any(type(rate) is not int or
                    (rate < 0 if sweep_field == "adversarial_rate" else rate <= 0)
                    for rate in rates)):
            qualifier = ("non-negative" if sweep_field == "adversarial_rate"
                         else "positive")
            raise ValueError(f"campaign: rates must be {qualifier} integers")
        if rates != sorted(set(rates)):
            raise ValueError("campaign: rates must be strictly increasing")

        variants: list[CampaignVariant] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("campaign: each variant must be a mapping")
            variant_name = item.get("name", "")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", variant_name):
                raise ValueError(f"campaign: invalid variant name {variant_name!r}")
            unknown = sorted(set(item) - {"name"} - _VARIANT_FIELDS)
            if unknown:
                raise ValueError(
                    f"campaign variant {variant_name}: unsupported fields {unknown}")
            variants.append(CampaignVariant(
                variant_name,
                {key: copy.deepcopy(value) for key, value in item.items()
                 if key != "name"},
            ))
        names = [variant.name for variant in variants]
        if not names:
            raise ValueError("campaign: at least one variant is required")
        if len(names) != len(set(names)):
            raise ValueError("campaign: variant names must be unique")

        warmup_s = raw.get("warmup_s", sweep_mod.WARMUP_S)
        window_s = raw.get("window_s", sweep_mod.WINDOW_S)
        drop_tolerance_pct = raw.get(
            "drop_tolerance_pct", sweep_mod.DROP_TOLERANCE_PCT)
        if type(warmup_s) is not int or type(window_s) is not int:
            raise ValueError("campaign: warmup_s and window_s must be integers")
        if (isinstance(drop_tolerance_pct, bool) or
                not isinstance(drop_tolerance_pct, (int, float))):
            raise ValueError("campaign: drop_tolerance_pct must be numeric")
        drop_tolerance_pct = float(drop_tolerance_pct)
        if warmup_s < 0 or window_s <= 0:
            raise ValueError("campaign: warmup_s must be >= 0 and window_s must be > 0")
        if not 0 <= drop_tolerance_pct < 100:
            raise ValueError("campaign: drop_tolerance_pct must be in [0, 100)")
        stop_on_drop = raw.get("stop_on_drop", True)
        if type(stop_on_drop) is not bool:
            raise ValueError("campaign: stop_on_drop must be boolean")
        strict_through_rate = raw.get("strict_through_rate")
        if (strict_through_rate is not None and
                (type(strict_through_rate) is not int or strict_through_rate <= 0)):
            raise ValueError(
                "campaign: strict_through_rate must be a positive integer or null")
        min_offered_throughput_pct = raw.get("min_offered_throughput_pct")
        if (min_offered_throughput_pct is not None and
                (isinstance(min_offered_throughput_pct, bool) or
                 not isinstance(min_offered_throughput_pct, (int, float)) or
                 not 0 < min_offered_throughput_pct <= 100)):
            raise ValueError(
                "campaign: min_offered_throughput_pct must be in (0, 100] or null")
        if min_offered_throughput_pct is not None:
            min_offered_throughput_pct = float(min_offered_throughput_pct)

        campaign = cls(
            name=name,
            output=output,
            sweep_field=sweep_field,
            rates=rates,
            warmup_s=warmup_s,
            window_s=window_s,
            drop_tolerance_pct=drop_tolerance_pct,
            stop_on_drop=stop_on_drop,
            strict_through_rate=strict_through_rate,
            min_offered_throughput_pct=min_offered_throughput_pct,
            committee_sizes=committee_sizes,
            base=copy.deepcopy(base),
            variants=variants,
        )
        for nodes in campaign.committee_sizes:
            campaign.configs(nodes=nodes)
        return campaign

    def configs(self, only: set[str] | None = None,
                nodes: int | None = None) -> list[tuple[str, RunConfig]]:
        if nodes is None:
            if len(self.committee_sizes) != 1:
                raise ValueError(
                    "campaign: select one committee size when building configs")
            nodes = self.committee_sizes[0]
        if nodes not in self.committee_sizes:
            raise ValueError(f"campaign: unknown committee size {nodes}")
        known = {variant.name for variant in self.variants}
        if only:
            missing = sorted(only - known)
            if missing:
                raise ValueError(f"campaign: unknown selected variants {missing}")

        configs: list[tuple[str, RunConfig]] = []
        for variant in self.variants:
            if only and variant.name not in only:
                continue
            values = copy.deepcopy(self.base)
            values.update(copy.deepcopy(variant.overrides))
            values["nodes"] = nodes
            values["run_id"] = self.name
            values[self.sweep_field] = self.rates[0]
            values["duration_s"] = self.warmup_s + self.window_s
            cfg = RunConfig.from_dict(values)
            if cfg.fault.kind != "none":
                raise ValueError("campaign variants cannot inject faults")
            invalid = []
            for value in self.rates:
                probe = copy.deepcopy(cfg)
                setattr(probe, self.sweep_field, value)
                try:
                    probe.validate()
                except ValueError:
                    invalid.append(value)
            if invalid:
                raise ValueError(
                    f"campaign: {self.sweep_field} values {invalid} are invalid for "
                    f"the effective workload")
            configs.append((variant.name, cfg))
        return configs

    def for_committee(self, nodes: int) -> "CampaignConfig":
        """Return one independently provisioned committee campaign."""
        if nodes not in self.committee_sizes:
            raise ValueError(f"campaign: unknown committee size {nodes}")
        base = copy.deepcopy(self.base)
        base["nodes"] = nodes
        return dataclasses.replace(
            self,
            name=f"{self.name}-n{nodes}",
            output=str(pathlib.Path(self.output) / f"n-{nodes}"),
            committee_sizes=[nodes],
            base=base,
        )


def _prepare_configs(configs: list[tuple[str, RunConfig]]) -> dict[str, str]:
    """Validate local inputs and pin every registry image."""
    first = configs[0][1]
    key_path = pathlib.Path(first.ssh_key_path).expanduser()
    if not key_path.is_file():
        raise RuntimeError(f"SSH private key not found: {key_path}")

    for _name, cfg in configs:
        for path in cfg.dashboards:
            if not pathlib.Path(path).expanduser().is_file():
                raise RuntimeError(f"dashboard not found: {path}")
        if cfg.image_source == "build-on-control":
            repo = pathlib.Path(cfg.build_repo).expanduser()
            if not repo.is_dir():
                raise RuntimeError(f"build repository not found: {repo}")

    pinned: dict[str, str] = {}
    for _name, cfg in configs:
        if cfg.image_source != "registry":
            continue
        if cfg.image not in pinned:
            pinned[cfg.image] = images.pin_to_digest(cfg.image)[0]
        cfg.image = pinned[cfg.image]
    return pinned


def preflight(campaign: CampaignConfig, only: set[str] | None = None) -> tuple[
        list[tuple[str, RunConfig]], dict]:
    """Resolve immutable inputs and verify local and AWS prerequisites."""
    configs = campaign.configs(only)
    pinned = _prepare_configs(configs)
    first = configs[0][1]

    aws_report = Aws(first).preflight()
    measured_s = len(configs) * len(campaign.rates) * (
        campaign.warmup_s + campaign.window_s)
    report = {
        **aws_report,
        "name": campaign.name,
        "output": campaign.output,
        "nodes": first.nodes,
        "instances": first.nodes + 1,
        "instance_type": first.instance_type or "auto",
        "az": first.az or "auto-select one AZ",
        "sweep_field": campaign.sweep_field,
        "rates": campaign.rates,
        "warmup_s": campaign.warmup_s,
        "window_s": campaign.window_s,
        "stop_on_drop": campaign.stop_on_drop,
        "strict_through_rate": campaign.strict_through_rate,
        "min_offered_throughput_pct": campaign.min_offered_throughput_pct,
        "variants": [name for name, _cfg in configs],
        "images": pinned,
        "minimum_duration_s": measured_s,
        "minimum_instance_hours": (first.nodes + 1) * measured_s / 3600,
    }
    return configs, report


def print_plan(report: dict) -> None:
    hours = report["minimum_duration_s"] / 3600
    print(f"campaign: {report['name']} (plan only; no resources created)")
    print(f"aws: {report['arn']} in account {report['account']}")
    if fleets := report.get("sequential_fleets"):
        sequence = ", ".join(
            f"n={fleet['nodes']} ({fleet['instances']} instances)"
            for fleet in fleets
        )
        print(f"fleets: sequential {sequence}")
        print(f"hardware: {report['instance_type']} in {report['az']}; "
              f"peak {report['instances']} instances")
    else:
        print(f"fleet: {report['instances']} x {report['instance_type']} in "
              f"{report['az']}")
    print(f"variants: {', '.join(report['variants'])}")
    print(f"{report['sweep_field']}: "
          f"{','.join(str(rate) for rate in report['rates'])} tx/s")
    print(f"timing: {report['warmup_s']}s warmup + {report['window_s']}s window; "
          f"at least {hours:.1f}h total")
    strict = report["strict_through_rate"]
    print("validation: strict at all points" if strict is None else
          f"validation: strict through {strict:,} {report['sweep_field']} tx/s; "
          "exploratory above")
    print(f"early stop: {'on' if report['stop_on_drop'] else 'off'}")
    floor = report["min_offered_throughput_pct"]
    if floor is not None:
        print(f"overload stop: committed throughput below {floor:g}% of reachable load")
    print(f"minimum fleet usage: {report['minimum_instance_hours']:.1f} instance-hours")
    quota = report.get("quota")
    if quota and quota["available_vcpus"] is None:
        print(f"quota: needs {quota['required_vcpus']} vCPUs; not readable with current IAM "
              f"permissions ({quota['quota_code']})")
    elif quota:
        print(f"quota: {quota['required_vcpus']} required / "
              f"{quota['available_vcpus']:g} available vCPUs")
    print(f"output: {report['output']}")
    for original, pinned in report["images"].items():
        print(f"image: {original} -> {pinned}")
    print("execute: add --execute")


def _checkpoint(path: pathlib.Path, state: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, allow_nan=False))
    temporary.replace(path)


def _definition(campaign: CampaignConfig,
                configs: list[tuple[str, RunConfig]]) -> dict:
    return {
        "name": campaign.name,
        "sweep_field": campaign.sweep_field,
        "rates": campaign.rates,
        "warmup_s": campaign.warmup_s,
        "window_s": campaign.window_s,
        "drop_tolerance_pct": campaign.drop_tolerance_pct,
        "stop_on_drop": campaign.stop_on_drop,
        "strict_through_rate": campaign.strict_through_rate,
        "min_offered_throughput_pct": campaign.min_offered_throughput_pct,
        "variants": [
            {"name": name, "effective_config": dataclasses.asdict(cfg)}
            for name, cfg in configs
        ],
    }


def _new_state(campaign: CampaignConfig,
               configs: list[tuple[str, RunConfig]]) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "schema_version": 1,
        "name": campaign.name,
        "status": "running",
        "error": None,
        "started_at": now,
        "finished_at": None,
        "sweep_field": campaign.sweep_field,
        "rates": campaign.rates,
        "warmup_s": campaign.warmup_s,
        "window_s": campaign.window_s,
        "definition": _definition(campaign, configs),
        "fleet": None,
        "monitoring_archive": None,
        "monitoring_archives": [],
        "monitoring_archive_error": None,
        "monitoring_bundle": None,
        "monitoring_bundle_error": None,
        "cleanup_warnings": [],
        "teardown_s": None,
        "teardown_error": None,
        "variants": [
            {
                "name": name,
                "protocol": cfg.protocol,
                "status": "pending",
                "error": None,
                "output": name,
                "effective_config": dataclasses.asdict(cfg),
            }
            for name, cfg in configs
        ],
    }


def _load_state(path: pathlib.Path, campaign: CampaignConfig,
                configs: list[tuple[str, RunConfig]], resume: bool) -> dict:
    if not path.exists():
        if resume:
            raise RuntimeError(f"campaign state does not exist: {path}")
        return _new_state(campaign, configs)
    if not resume:
        raise RuntimeError(f"campaign output already exists: {path}; use --resume")
    state = json.loads(path.read_text())
    if state.get("schema_version") != 1:
        raise RuntimeError(f"unsupported campaign state schema: "
                           f"{state.get('schema_version')!r}")
    if state.get("name") != campaign.name:
        raise RuntimeError(
            f"campaign state is for {state.get('name')!r}, not {campaign.name!r}")
    expected = [name for name, _cfg in configs]
    actual = [variant["name"] for variant in state.get("variants", [])]
    if actual != expected:
        raise RuntimeError(
            f"resume variant set differs: state has {actual}, config has {expected}")
    if state.get("definition") != _definition(campaign, configs):
        raise RuntimeError(
            "campaign definition changed; resume with the original settings and "
            "image digests")
    for variant in state["variants"]:
        if variant["status"] == "running":
            variant["status"] = "pending"
            variant["error"] = None
    if "monitoring_archives" not in state:
        archive = state.get("monitoring_archive")
        state["monitoring_archives"] = [archive] if archive else []
    state["status"] = "running"
    state["error"] = None
    state["finished_at"] = None
    state["cleanup_warnings"] = []
    state["teardown_error"] = None
    return state


def _reset_protocol_state(ssh: Ssh, hosts: list[Host], control: Host) -> None:
    command = (
        "sudo bash -lc 'docker rm -f wanbench-node 2>/dev/null || true; "
        "find /opt/wanbench -mindepth 1 -delete; "
        "mkdir -p /opt/wanbench; chown ${SUDO_UID:-0} /opt/wanbench'"
    )
    ssh.fanout(hosts, command)
    ssh.run(control, command)


def _apply_fleet_info(cfg: RunConfig, fleet_cfg: RunConfig) -> None:
    cfg.az = fleet_cfg.az
    cfg.instance_type = fleet_cfg.instance_type
    cfg.ami = fleet_cfg.ami
    cfg.grafana_open_cidr = fleet_cfg.grafana_open_cidr
    cfg.ssh_open_cidr = fleet_cfg.ssh_open_cidr


def _image_key(cfg: RunConfig) -> tuple[str, str, str, str]:
    return (cfg.image_source, cfg.image, cfg.build_repo, cfg.build_dockerfile)


def execute(campaign: CampaignConfig, configs: list[tuple[str, RunConfig]],
            outdir: str, resume: bool = False) -> dict:
    """Execute pending variants sequentially on one fleet."""
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "campaign.json"
    if not state_path.exists() and any(out.iterdir()):
        raise RuntimeError(
            f"campaign output is not empty and has no state file: {out}")
    state = _load_state(state_path, campaign, configs, resume)
    _checkpoint(state_path, state)

    pending = [
        (name, cfg, entry)
        for (name, cfg), entry in zip(configs, state["variants"])
        if entry["status"] == "pending"
    ]
    if not pending:
        state["status"] = (
            "completed_with_failures"
            if any(entry["status"] == "failed" for entry in state["variants"])
            else "completed"
        )
        state["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _checkpoint(state_path, state)
        print(f"campaign: no pending variants -> {state_path}")
        return state

    fleet_cfg = copy.deepcopy(pending[0][1])
    fleet_cfg.keep_monitoring_on_down = False
    fleet_cfg.dashboards = list(dict.fromkeys(
        path for _name, cfg in configs for path in cfg.dashboards))
    fleet_image_key = _image_key(fleet_cfg)

    fleet = None
    try:
        fleet = up(fleet_cfg)
        aws, ssh, hosts, control = fleet
        info = aws.fleet_info()
        fleet_cfg.az = info["az"]
        fleet_cfg.instance_type = info["instance_type"]
        fleet_cfg.ami = info["ami"]
        state["fleet"] = {
            **info,
            "region": fleet_cfg.region,
            "instances": fleet_cfg.nodes + 1,
        }
        for _name, cfg in configs:
            _apply_fleet_info(cfg, fleet_cfg)

        prepared = {fleet_image_key: fleet_cfg.image}
        for _name, cfg, _entry in pending:
            key = _image_key(cfg)
            if key in prepared:
                cfg.image = prepared[key]
                continue
            ref = images.ensure_image(ssh, cfg, control, hosts)
            prepare.pull_image(ssh, hosts, ref)
            cfg.image = ref
            prepared[key] = ref

        for name, cfg, entry in pending:
            entry["status"] = "running"
            entry["effective_config"] = dataclasses.asdict(cfg)
            _checkpoint(state_path, state)
            try:
                print(f"campaign: variant {name} ({cfg.protocol})")
                _reset_protocol_state(ssh, hosts, control)
                metric_labels = {"wanbench_variant": name}
                targets = monitoring.validator_targets(cfg, hosts, metric_labels)
                try:
                    monitoring.configure_targets(ssh, cfg, control, targets)
                except Exception as exc:
                    print(f"campaign: WARNING monitoring targets unchanged ({exc})",
                          flush=True)
                sweep_mod.sweep(
                    cfg,
                    campaign.rates,
                    str(out / name),
                    sweep_field=campaign.sweep_field,
                    warmup_s=campaign.warmup_s,
                    window_s=campaign.window_s,
                    drop_tolerance_pct=campaign.drop_tolerance_pct,
                    stop_on_drop=campaign.stop_on_drop,
                    strict_through_rate=campaign.strict_through_rate,
                    min_offered_throughput_pct=(
                        campaign.min_offered_throughput_pct
                    ),
                    metric_labels=metric_labels,
                    fleet=fleet,
                )
            except Exception as exc:  # A protocol failure must not cancel its peers.
                entry["status"] = "failed"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                _checkpoint(state_path, state)
                print(f"campaign: variant {name} failed ({entry['error']}); "
                      "continuing", flush=True)
            else:
                entry["status"] = "completed"
                entry["error"] = None
                _checkpoint(state_path, state)
    except BaseException as exc:
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}"
        _checkpoint(state_path, state)
        raise
    finally:
        if fleet is not None:
            _aws, ssh, hosts, _control = fleet
            teardown_start = time.monotonic()
            try:
                try:
                    archives = state.setdefault("monitoring_archives", [])
                    archive_name = (
                        "prometheus-tsdb.tar.gz" if not archives
                        else f"prometheus-tsdb-part{len(archives) + 1}.tar.gz"
                    )
                    archive = monitoring.archive_prometheus(
                        ssh, _control, out, filename=archive_name)
                    archives.append(archive.name)
                    state["monitoring_archive"] = archives[0]
                except Exception as exc:  # noqa: BLE001 -- teardown must still run
                    state["monitoring_archive_error"] = f"{type(exc).__name__}: {exc}"
                    print(f"campaign: WARNING Prometheus archive failed ({exc})", flush=True)
                for label, cleanup in (
                    ("fault cleanup", lambda: faults.clear(ssh, hosts)),
                    ("WAN cleanup", lambda: prepare.clear_wan(ssh, hosts)),
                ):
                    try:
                        cleanup()
                    except Exception as exc:  # noqa: BLE001 -- fleet deletion is authoritative
                        warning = f"{label}: {type(exc).__name__}: {exc}"
                        state["cleanup_warnings"].append(warning)
                        print(f"campaign: WARNING {warning}", flush=True)
                down(fleet_cfg, keep_monitoring=False)
            except BaseException as exc:
                state["teardown_error"] = f"{type(exc).__name__}: {exc}"
                state["status"] = "failed"
                if state.get("error") is None:
                    state["error"] = state["teardown_error"]
                    raise
                print(f"campaign: WARNING teardown also failed ({exc})", flush=True)
            finally:
                state["teardown_s"] = round(time.monotonic() - teardown_start)
                _checkpoint(state_path, state)
    state["status"] = (
        "completed_with_failures"
        if any(entry["status"] == "failed" for entry in state["variants"])
        else "completed"
    )
    state["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    try:
        bundle = monitoring.write_archive_bundle(
            out,
            state.get("started_at"),
            state.get("finished_at"),
            fleet_cfg.dashboards,
        )
        state["monitoring_bundle"] = bundle.name
        state["monitoring_bundle_error"] = None
    except Exception as exc:  # noqa: BLE001 -- result summaries remain usable
        state["monitoring_bundle_error"] = f"{type(exc).__name__}: {exc}"
        print(f"campaign: WARNING monitoring bundle failed ({exc})", flush=True)
    _checkpoint(state_path, state)
    return state


def run(path: str, outdir: str | None = None, execute_run: bool = False,
        resume: bool = False, only: set[str] | None = None) -> dict:
    campaign = CampaignConfig.load(path)
    if len(campaign.committee_sizes) > 1:
        from . import matrix
        groups, report = matrix.preflight(campaign, only)
        if outdir:
            report["output"] = outdir
        if not execute_run:
            print_plan(report)
            return report
        output = outdir or campaign.output
        return matrix.execute_matrix(campaign, groups, output, resume=resume)
    configs, report = preflight(campaign, only)
    if outdir:
        report["output"] = outdir
    if not execute_run:
        print_plan(report)
        return report
    output = outdir or campaign.output
    return execute(campaign, configs, output, resume=resume)
