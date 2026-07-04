#!/usr/bin/env python3
"""Backup Docker volume data to an rclone remote.

The script is intentionally provider-agnostic: configure your bucket provider in
rclone, then point this script at the remote path you want to use.

It supports:
- Docker named volumes
- Additional host paths
- Periodic execution via an internal sleep loop
- Retention cleanup on the remote via rclone
- JSON config with environment variable overrides

Important: for live databases, a filesystem-level volume backup may not be
transactionally consistent unless the workload is quiesced or you also perform a
logical database dump.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Iterable, Sequence


@dataclasses.dataclass(slots=True)
class Config:
    docker_volumes: list[str]
    extra_paths: list[str]
    remote: str
    remote_prefix: str
    interval_minutes: int
    retention_days: int
    rclone_bin: str
    docker_bin: str
    compression_level: int
    keep_archives_local: bool
    work_dir: str
    config_path: str | None

    @classmethod
    def from_sources(cls, args: argparse.Namespace) -> "Config":
        file_config = load_json_config(args.config)

        def pick(name: str, env_name: str, default):
            if getattr(args, name) is not None:
                return getattr(args, name)
            if env_name in os.environ and os.environ[env_name] != "":
                value = os.environ[env_name]
                if isinstance(default, bool):
                    return value.lower() in {"1", "true", "yes", "on"}
                if isinstance(default, int):
                    return int(value)
                if isinstance(default, list):
                    return [item for item in value.split(",") if item]
                return value
            if name in file_config:
                return file_config[name]
            return default

        docker_volumes = pick("docker_volumes", "BACKUP_DOCKER_VOLUMES", file_config.get("docker_volumes", []))
        extra_paths = pick("extra_paths", "BACKUP_EXTRA_PATHS", file_config.get("extra_paths", []))

        remote = pick("remote", "BACKUP_REMOTE", file_config.get("remote", ""))
        if not remote:
            raise SystemExit("remote backup target is required (set remote in config or BACKUP_REMOTE)")

        remote_prefix = pick("remote_prefix", "BACKUP_REMOTE_PREFIX", file_config.get("remote_prefix", "docker-backups"))
        interval_minutes = pick("interval_minutes", "BACKUP_INTERVAL_MINUTES", file_config.get("interval_minutes", 1440))
        retention_days = pick("retention_days", "BACKUP_RETENTION_DAYS", file_config.get("retention_days", 14))
        rclone_bin = pick("rclone_bin", "BACKUP_RCLONE_BIN", file_config.get("rclone_bin", "rclone"))
        docker_bin = pick("docker_bin", "BACKUP_DOCKER_BIN", file_config.get("docker_bin", "docker"))
        compression_level = pick("compression_level", "BACKUP_COMPRESSION_LEVEL", file_config.get("compression_level", 6))
        keep_archives_local = pick("keep_archives_local", "BACKUP_KEEP_LOCAL_ARCHIVES", file_config.get("keep_archives_local", False))
        work_dir = pick("work_dir", "BACKUP_WORK_DIR", file_config.get("work_dir", "./backups"))

        return cls(
            docker_volumes=list(docker_volumes),
            extra_paths=list(extra_paths),
            remote=str(remote),
            remote_prefix=str(remote_prefix).strip("/"),
            interval_minutes=int(interval_minutes),
            retention_days=int(retention_days),
            rclone_bin=str(rclone_bin),
            docker_bin=str(docker_bin),
            compression_level=int(compression_level),
            keep_archives_local=bool(keep_archives_local),
            work_dir=str(work_dir),
            config_path=args.config,
        )


def load_json_config(path: str | None) -> dict:
    if not path:
        return {}
    config_path = Path(path)
    if not config_path.exists():
        raise SystemExit(f"config file not found: {config_path}")
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON config in {config_path}: {exc}") from exc


def ensure_tools(cfg: Config) -> None:
    for binary in (cfg.docker_bin, cfg.rclone_bin):
        if shutil.which(binary) is None:
            raise SystemExit(f"required binary not found on PATH: {binary}")


def run_cmd(cmd: Sequence[str], *, capture_output: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        check=True,
        text=True,
        capture_output=capture_output,
    )


def docker_volume_mountpoint(docker_bin: str, volume_name: str) -> Path:
    result = run_cmd([docker_bin, "volume", "inspect", volume_name, "--format", "{{.Mountpoint}}"])
    mountpoint = result.stdout.strip()
    if not mountpoint:
        raise RuntimeError(f"volume has no mountpoint: {volume_name}")
    path = Path(mountpoint)
    if not path.exists():
        raise RuntimeError(f"volume mountpoint does not exist: {path}")
    return path


def docker_volumes_in_use(docker_bin: str) -> list[str]:
    containers = run_cmd([docker_bin, "ps", "-q", "--no-trunc"]).stdout.splitlines()
    container_ids = [container_id.strip() for container_id in containers if container_id.strip()]
    if not container_ids:
        return []

    inspect = run_cmd([
        docker_bin,
        "inspect",
        *container_ids,
        "--format",
        "{{range .Mounts}}{{if eq .Type \"volume\"}}{{println .Name}}{{end}}{{end}}",
    ]).stdout.splitlines()
    return sorted({name.strip() for name in inspect if name.strip()})


def resolve_docker_volumes(docker_bin: str, configured_volumes: Iterable[str]) -> list[str]:
    values = [value.strip() for value in configured_volumes if value.strip()]
    if "*" not in values:
        return list(dict.fromkeys(values))

    discovered = docker_volumes_in_use(docker_bin)
    explicit = [value for value in values if value != "*"]
    return sorted(set(explicit).union(discovered))


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)


def create_archive(source: Path, target: Path, arcname: str, compression_level: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, mode=f"w:gz", compresslevel=compression_level) as tar:
        tar.add(source, arcname=arcname)


@dataclasses.dataclass(slots=True)
class ArchiveSpec:
    label: str
    source: Path
    arcname: str


def build_archive_specs(cfg: Config) -> list[ArchiveSpec]:
    specs: list[ArchiveSpec] = []
    for volume_name in resolve_docker_volumes(cfg.docker_bin, cfg.docker_volumes):
        mountpoint = docker_volume_mountpoint(cfg.docker_bin, volume_name)
        specs.append(ArchiveSpec(label=f"volume:{volume_name}", source=mountpoint, arcname=volume_name))
    for path_str in cfg.extra_paths:
        path = Path(path_str).expanduser().resolve()
        if not path.exists():
            raise RuntimeError(f"extra path does not exist: {path}")
        specs.append(ArchiveSpec(label=f"path:{path.name}", source=path, arcname=path.name))
    if not specs:
        raise RuntimeError("no docker volumes or extra paths were configured")
    return specs


def archive_name(cfg: Config, spec: ArchiveSpec, stamp: str) -> str:
    return f"{stamp}-{safe_name(spec.label)}.tar.gz"


def remote_base(cfg: Config) -> str:
    base = cfg.remote.rstrip("/")
    if cfg.remote_prefix:
        return f"{base}/{cfg.remote_prefix.strip('/')}"
    return base


def upload_archive(cfg: Config, archive_path: Path, remote_name: str) -> None:
    destination = f"{remote_base(cfg)}/{remote_name}"
    run_cmd([cfg.rclone_bin, "copyto", str(archive_path), destination])


def prune_remote(cfg: Config) -> None:
    if cfg.retention_days <= 0:
        return
    cutoff = f"{cfg.retention_days}d"
    run_cmd([
        cfg.rclone_bin,
        "delete",
        f"{remote_base(cfg)}",
        "--min-age",
        cutoff,
    ])


def run_backup_once(cfg: Config) -> list[str]:
    work_dir = Path(cfg.work_dir).expanduser().resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    stamp = timestamp()
    created: list[str] = []
    specs = build_archive_specs(cfg)

    with tempfile.TemporaryDirectory(prefix="docker-backup-", dir=str(work_dir)) as temp_dir:
        temp_root = Path(temp_dir)
        for spec in specs:
            archive_file = temp_root / archive_name(cfg, spec, stamp)
            create_archive(spec.source, archive_file, spec.arcname, cfg.compression_level)
            upload_archive(cfg, archive_file, archive_file.name)
            created.append(archive_file.name)
            if cfg.keep_archives_local:
                final_path = work_dir / archive_file.name
                shutil.copy2(archive_file, final_path)

    prune_remote(cfg)
    write_manifest(work_dir, stamp, cfg, created)
    return created


def write_manifest(work_dir: Path, stamp: str, cfg: Config, created: list[str]) -> None:
    manifest = {
        "timestamp_utc": stamp,
        "remote": cfg.remote,
        "remote_prefix": cfg.remote_prefix,
        "docker_volumes": cfg.docker_volumes,
        "extra_paths": cfg.extra_paths,
        "archives": created,
    }
    (work_dir / f"{stamp}-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Back up Docker volume data to an rclone remote")
    parser.add_argument("--config", help="Path to a JSON config file")
    parser.add_argument("--remote", help="Override the remote bucket path, for example s3:bucket/backups")
    parser.add_argument("--remote-prefix", help="Override the prefix under the remote")
    parser.add_argument("--docker-volume", dest="docker_volumes", action="append", help="Docker volume name to include; repeatable")
    parser.add_argument("--extra-path", dest="extra_paths", action="append", help="Additional host path to include; repeatable")
    parser.add_argument("--interval-minutes", type=int, help="Run repeatedly with this sleep interval")
    parser.add_argument("--retention-days", type=int, help="Delete remote archives older than this many days")
    parser.add_argument("--rclone-bin", help="Path to rclone binary")
    parser.add_argument("--docker-bin", help="Path to docker binary")
    parser.add_argument("--compression-level", type=int, help="gzip compression level (0-9)")
    parser.add_argument("--keep-archives-local", action="store_true", help="Keep a local copy of archives in work_dir")
    parser.add_argument("--work-dir", help="Directory for temporary and optional local archive files")
    parser.add_argument("--once", action="store_true", help="Run a single backup and exit")
    return parser.parse_args(argv)


def main(argv: Sequence[str]) -> int:
    args = parse_args(argv)
    cfg = Config.from_sources(args)
    ensure_tools(cfg)

    if args.remote is not None:
        cfg.remote = args.remote
    if args.remote_prefix is not None:
        cfg.remote_prefix = args.remote_prefix.strip("/")
    if args.docker_volumes is not None:
        cfg.docker_volumes = args.docker_volumes
    if args.extra_paths is not None:
        cfg.extra_paths = args.extra_paths
    if args.interval_minutes is not None:
        cfg.interval_minutes = args.interval_minutes
    if args.retention_days is not None:
        cfg.retention_days = args.retention_days
    if args.rclone_bin is not None:
        cfg.rclone_bin = args.rclone_bin
    if args.docker_bin is not None:
        cfg.docker_bin = args.docker_bin
    if args.compression_level is not None:
        cfg.compression_level = args.compression_level
    if args.keep_archives_local:
        cfg.keep_archives_local = True
    if args.work_dir is not None:
        cfg.work_dir = args.work_dir

    if args.once or cfg.interval_minutes <= 0:
        run_backup_once(cfg)
        return 0

    while True:
        start = time.time()
        try:
            run_backup_once(cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"backup failed: {exc}", file=sys.stderr)
        elapsed = time.time() - start
        sleep_for = max(1, cfg.interval_minutes * 60 - int(elapsed))
        time.sleep(sleep_for)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
