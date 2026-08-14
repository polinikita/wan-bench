"""`wanbench` command-line entry point."""

from __future__ import annotations

import argparse
import signal
import sys

from . import campaign as campaign_mod
from . import faults, prepare
from . import sweep as sweep_mod
from .aws import Aws
from .collect import collect
from .config import RunConfig
from .deploy import deploy
from .run import down, run, up
from .ssh import Ssh


class Killed(KeyboardInterrupt):
    """A termination signal that reaches fleet teardown handlers."""


def _install_signal_teardown() -> None:
    """Convert catchable termination signals into teardown-safe exceptions."""
    def handler(signum, _frame):
        raise Killed(f"received signal {signal.Signals(signum).name}")

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # Host-side termination still limits orphan lifetime.
            pass


def _cfg(args) -> RunConfig:
    cfg = RunConfig.load(args.config)
    if getattr(args, "no_state_sync", False):
        cfg.sequence_checkpoints = False
        cfg.sequence_install_enabled = False
    return cfg


def _ctx(cfg: RunConfig):
    """Re-attach to an already-provisioned run without re-launching."""
    aws = Aws(cfg)
    ssh = Ssh(cfg.ssh_key_path, cfg.ssh_user)
    hosts = aws.describe()[: cfg.nodes]
    control = aws.control_host()
    return aws, ssh, hosts, control


def main(argv=None) -> int:
    _install_signal_teardown()
    p = argparse.ArgumentParser(prog="wanbench")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name in ("up", "deploy", "collect", "down"):
        sp = sub.add_parser(name)
        sp.add_argument("--config", required=True)
        if name == "collect":
            sp.add_argument("--out", required=True)
        if name == "deploy":
            sp.add_argument("--no-state-sync", action="store_true",
                            help="disable Vantage sequence checkpoint state sync")
        if name == "down":
            sp.add_argument("--keep-monitoring", dest="keep_monitoring",
                            action="store_true", default=None)
            sp.add_argument("--no-keep-monitoring", dest="keep_monitoring",
                            action="store_false")

    sp = sub.add_parser("run")
    sp.add_argument("--config", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--no-state-sync", action="store_true",
                    help="disable Vantage sequence checkpoint state sync for this run")

    sp = sub.add_parser("sweep")
    sp.add_argument("--config", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--rates", required=True,
                    help="comma-separated AGGREGATE tx/s, strictly increasing; the "
                         "sweep stops early after a material committed-TPS decrease")
    sp.add_argument("--warmup", type=int, default=sweep_mod.WARMUP_S,
                    help="untimed run-in seconds per point")
    sp.add_argument("--window", type=int, default=sweep_mod.WINDOW_S,
                    help="measured steady-state seconds per point")
    sp.add_argument("--drop-tolerance-pct", type=float,
                    default=sweep_mod.DROP_TOLERANCE_PCT,
                    help="material committed-TPS drop required for early stop")
    sp.add_argument("--no-early-stop", action="store_true",
                    help="continue after committed throughput decreases")
    sp.add_argument("--strict-through-rate", type=int, default=None,
                    help="use exploratory validation above this aggregate rate")
    sp.add_argument("--min-offered-throughput-pct", type=float, default=None,
                    help="stop when committed throughput falls below this percentage "
                         "of reachable offered load")
    sp.add_argument("--no-state-sync", action="store_true",
                    help="disable Vantage sequence checkpoint state sync for this sweep")

    sp = sub.add_parser("campaign")
    sp.add_argument("--config", required=True)
    sp.add_argument("--out", default=None)
    sp.add_argument("--execute", action="store_true",
                    help="create resources and run; default is a read-only plan")
    sp.add_argument("--resume", action="store_true",
                    help="resume incomplete variants from campaign.json")
    sp.add_argument("--only", default=None,
                    help="comma-separated variant names")

    fp = sub.add_parser("fault")
    fp.add_argument("--config", required=True)
    fsub = fp.add_subparsers(dest="fkind", required=True)
    c = fsub.add_parser("crash"); c.add_argument("--nodes", required=True)
    rs = fsub.add_parser("restart"); rs.add_argument("--nodes", required=True)
    r = fsub.add_parser("ring"); r.add_argument("--pct", type=int, default=10)
    r.add_argument("--mode", default="cut")
    s = fsub.add_parser("split")
    s.add_argument("--group-a", required=True); s.add_argument("--group-b", required=True)
    s.add_argument("--mode", default="cut")
    b = fsub.add_parser("blip"); b.add_argument("--node", type=int, required=True)
    b.add_argument("--mode", default="cut")
    fsub.add_parser("clear")

    n = sub.add_parser("nuke")
    n.add_argument("--region", required=True); n.add_argument("--profile", default=None)

    args = p.parse_args(argv)

    if args.cmd == "up":
        up(_cfg(args)); return 0
    if args.cmd == "down":
        down(_cfg(args), keep_monitoring=args.keep_monitoring); return 0
    if args.cmd == "run":
        run(_cfg(args), args.out); return 0
    if args.cmd == "sweep":
        rates = [int(x) for x in args.rates.split(",")]
        sweep_mod.sweep(_cfg(args), rates, args.out,
                        warmup_s=args.warmup, window_s=args.window,
                        drop_tolerance_pct=args.drop_tolerance_pct,
                        stop_on_drop=not args.no_early_stop,
                        strict_through_rate=args.strict_through_rate,
                        min_offered_throughput_pct=(
                            args.min_offered_throughput_pct
                        )); return 0
    if args.cmd == "campaign":
        if args.resume and not args.execute:
            p.error("campaign --resume requires --execute")
        only = ({name.strip() for name in args.only.split(",") if name.strip()}
                if args.only else None)
        if args.only and not only:
            p.error("campaign --only needs at least one variant name")
        campaign_mod.run(args.config, args.out, args.execute, args.resume, only)
        return 0
    if args.cmd == "nuke":
        ids = Aws.nuke(args.region, args.profile)
        print(f"nuke: terminated {len(ids)}: {ids}"); return 0
    if args.cmd == "deploy":
        cfg = _cfg(args); _, ssh, hosts, control = _ctx(cfg)
        deploy(ssh, cfg, control, hosts); return 0
    if args.cmd == "collect":
        cfg = _cfg(args); _, ssh, hosts, control = _ctx(cfg)
        collect(ssh, cfg, control, hosts, args.out); return 0
    if args.cmd == "fault":
        cfg = _cfg(args); _, ssh, hosts, _ = _ctx(cfg)
        return _fault(cfg, ssh, hosts, args)
    return 1


def _fault(cfg, ssh, hosts, args) -> int:
    if args.fkind == "crash":
        faults.crash(ssh, hosts, [int(x) for x in args.nodes.split(",")])
    elif args.fkind == "restart":
        faults.restart(ssh, hosts, [int(x) for x in args.nodes.split(",")])
    elif args.fkind == "ring":
        faults.ring(ssh, cfg, hosts, args.pct, args.mode)
    elif args.fkind == "split":
        faults.split(ssh, cfg, hosts, _range(args.group_a), _range(args.group_b), args.mode)
    elif args.fkind == "blip":
        faults.blip_on(ssh, cfg, hosts, args.node, None, args.mode)
    elif args.fkind == "clear":
        faults.clear(ssh, hosts)
    return 0


def _range(spec: str) -> list[int]:
    """Parse '5-9' or '0,2,4' into a list of indices."""
    if "-" in spec:
        a, b = spec.split("-"); return list(range(int(a), int(b) + 1))
    return [int(x) for x in spec.split(",")]


if __name__ == "__main__":
    sys.exit(main())
