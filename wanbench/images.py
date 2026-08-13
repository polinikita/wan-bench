"""Resolve, build, and distribute validator images."""

from __future__ import annotations

import pathlib
import subprocess
import tempfile

from .config import RunConfig
from .ssh import Host, Ssh

REGISTRY_PORT = 5000


def prepared_ref(cfg: RunConfig, control: Host) -> str:
    if cfg.image_source == "build-on-control":
        name = cfg.image.split("/")[-1]  # local name -> registry path
        return f"{control.private_ip}:{REGISTRY_PORT}/{name}"
    return cfg.image


def verify_registry_manifest(ref: str) -> str | None:
    """Verify anonymous pull access and return the registry digest when available."""
    import json as _json
    import re as _re
    import urllib.error
    import urllib.request

    host, _, rest = ref.partition("/")
    name, _, tag = rest.rpartition(":")
    if not (host and name and tag):
        raise RuntimeError(f"image ref {ref!r} is not <registry>/<name>:<tag>")

    def _head(url: str, token: str | None) -> str | None:
        req = urllib.request.Request(url, method="HEAD", headers={
            "Accept": "application/vnd.oci.image.index.v1+json, "
                      "application/vnd.docker.distribution.manifest.list.v2+json, "
                      "application/vnd.oci.image.manifest.v1+json, "
                      "application/vnd.docker.distribution.manifest.v2+json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.headers.get("Docker-Content-Digest")

    manifest_url = f"https://{host}/v2/{name}/manifests/{tag}"
    try:
        try:
            return _head(manifest_url, None)
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            challenge = e.headers.get("WWW-Authenticate", "")
        fields = dict(_re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = fields.get("realm")
        if not realm:
            raise RuntimeError(f"registry {host} sent 401 without a token challenge")
        params = f"service={fields.get('service', host)}&scope=repository:{name}:pull"
        with urllib.request.urlopen(f"{realm}?{params}", timeout=15) as r:
            token = _json.load(r).get("token")
        return _head(manifest_url, token)
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"image {ref} is not anonymously pullable: registry answered "
            f"HTTP {e.code} {e.reason} for {manifest_url} -- push the tag (or fix "
            f"the ref) before provisioning") from e
    except OSError as e:
        raise RuntimeError(f"cannot reach registry {host} to verify {ref}: {e}") from e


def pin_to_digest(ref: str) -> tuple[str, str | None]:
    """Resolve a mutable image tag to one immutable digest."""
    if "@sha256:" in ref:
        return ref, ref.split("@", 1)[1]
    digest = verify_registry_manifest(ref)
    if not digest:
        return ref, None
    repo = ref.rpartition(":")[0]
    return f"{repo}@{digest}", digest


def ensure_image(ssh: Ssh, cfg: RunConfig, control: Host, nodes: list[Host]) -> str:
    """Make the image pullable by every node; return the ref to pull."""
    if cfg.image_source == "registry":
        return cfg.image
    if cfg.image_source != "build-on-control":
        raise ValueError(f"unknown image_source {cfg.image_source!r}")

    ref = prepared_ref(cfg, control)
    # Configure trust before starting the registry because Docker restarts.
    _allow_insecure_registry(ssh, [control, *nodes], control.private_ip)
    _run_registry(ssh, control)
    _build_and_push(ssh, cfg, control, ref)
    return ref


def _run_registry(ssh: Ssh, control: Host) -> None:
    ssh.sudo(control,
             "docker rm -f wanbench-registry 2>/dev/null || true; "
             f"docker run -d --restart always --name wanbench-registry "
             f"-p {REGISTRY_PORT}:5000 registry:2")


def _allow_insecure_registry(ssh: Ssh, hosts: list[Host], registry_ip: str) -> None:
    reg = f"{registry_ip}:{REGISTRY_PORT}"
    cmd = (f"mkdir -p /etc/docker && "
           f"python3 - <<'PY'\n"
           f"import json,os\n"
           f"p='/etc/docker/daemon.json'\n"
           f"d=json.load(open(p)) if os.path.exists(p) else {{}}\n"
           f"d.setdefault('insecure-registries',[])\n"
           f"'{reg}' in d['insecure-registries'] or d['insecure-registries'].append('{reg}')\n"
           f"json.dump(d,open(p,'w'))\n"
           f"PY\n"
           f"systemctl restart docker")
    ssh.fanout(hosts, lambda h: f"sudo bash -lc {_shq(cmd)}")


def _build_and_push(ssh: Ssh, cfg: RunConfig, control: Host, ref: str) -> None:
    if not cfg.build_repo:
        raise ValueError("build-on-control needs build_repo (local path to the repo)")
    repo = pathlib.Path(cfg.build_repo).expanduser()
    dockerfile = cfg.build_dockerfile or "docker-bench/Dockerfile"
    # Send only tracked source files.
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tf:
        subprocess.run(["git", "-C", str(repo), "archive", "--format=tar",
                        "-o", tf.name, "HEAD"], check=True)
        # `/opt` is root-owned even when `/opt/wanbench` is an instance-store
        # mount owned by the SSH user. Create the sibling staging directory as
        # root, then transfer its ownership to the same uid/gid as the mount.
        ssh.sudo(
            control,
            "rm -rf /opt/wanbench-src && "
            "install -d "
            "-o \"$(stat -c %u /opt/wanbench)\" "
            "-g \"$(stat -c %g /opt/wanbench)\" /opt/wanbench-src",
        )
        ssh.scp(control, tf.name, "/opt/wanbench-src/src.tar")
    # Protocol Dockerfiles require BuildKit.
    ssh.sudo(control,
             "cd /opt/wanbench-src && tar xf src.tar && "
             f"DOCKER_BUILDKIT=1 docker build -f {dockerfile} -t {ref} . && "
             f"docker push {ref}",
             timeout=3600)


def _shq(s: str) -> str:
    import shlex
    return shlex.quote(s)
