#!/usr/bin/env python3
"""Compare captured SPEC run-directory outputs without rebuilding or rerunning."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any


class VerifyError(Exception):
    """A user-facing verification error."""


REDIRECT_SHELL_OPS = {
    "stdin": "<",
    "stdout": ">",
    "stdout-append": ">>",
    "stderr": "2>",
    "stderr-append": "2>>",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run specinvoke compare.cmd for each run directory in a manifest.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--install-root",
        default="../",
        help="SPEC install root; relative paths are resolved from the current directory",
    )
    parser.add_argument(
        "--commands-file",
        default=str(Path(__file__).with_name("commands.static.full.json")),
        help="captured commands JSON",
    )
    parser.add_argument(
        "--suite", action="append", help="suite filter; may be repeated"
    )
    parser.add_argument(
        "--benchmark",
        action="append",
        help="benchmark include filter; case-insensitive substring match; may be repeated",
    )
    parser.add_argument(
        "--skip-benchmark",
        action="append",
        help="benchmark exclusion filter; case-insensitive substring match; may be repeated",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print compare commands without running them",
    )
    return parser.parse_args()


def resolve_cli_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def load_manifest(commands_file: Path) -> dict[str, Any]:
    try:
        data = json.loads(commands_file.read_text())
    except OSError as exc:
        raise VerifyError(
            f"failed to read commands file {commands_file}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise VerifyError(
            f"failed to parse commands file {commands_file}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise VerifyError("commands file must contain a top-level object")
    commands = data.get("commands")
    if not isinstance(commands, list):
        raise VerifyError("commands file must contain a top-level 'commands' list")
    return data


def filter_entries(
    entries: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    suites = set(args.suite or [])
    benchmark_filters = [value.casefold() for value in args.benchmark or []]
    skip_benchmark_filters = [value.casefold() for value in args.skip_benchmark or []]
    filtered = []

    for command in entries:
        if suites and command.get("suite") not in suites:
            continue
        benchmark = str(command.get("benchmark", ""))
        benchmark_folded = benchmark.casefold()
        if benchmark_filters and not any(
            value in benchmark_folded for value in benchmark_filters
        ):
            continue
        if skip_benchmark_filters and any(
            value in benchmark_folded for value in skip_benchmark_filters
        ):
            continue
        filtered.append(command)

    if not filtered:
        raise VerifyError("no commands matched the requested filters")
    return filtered


def resolve_install_path(value: str, install_root: Path) -> Path:
    if value.startswith("./"):
        return (install_root / value[2:]).resolve(strict=False)
    if os.path.isabs(value):
        return Path(value)
    return (install_root / value).resolve(strict=False)


def cwd_relative_if_inside(path: Path, cwd: Path) -> str:
    resolved_path = path.resolve(strict=False)
    resolved_cwd = cwd.resolve(strict=False)
    try:
        relative = resolved_path.relative_to(resolved_cwd)
    except ValueError:
        return str(resolved_path)
    return relative.as_posix() or "."


def resolve_argv_path(value: str, install_root: Path, cwd: Path) -> str:
    if value.startswith("./"):
        return cwd_relative_if_inside(install_root / value[2:], cwd)
    if os.path.isabs(value):
        return cwd_relative_if_inside(Path(value), cwd)
    return value


def resolve_argv_value(value: str, install_root: Path, cwd: Path) -> str:
    if value.startswith("-I./"):
        return "-I" + resolve_argv_path(value[2:], install_root, cwd)
    if value.startswith("-L./"):
        return "-L" + resolve_argv_path(value[2:], install_root, cwd)
    if "=" in value:
        prefix, suffix = value.split("=", 1)
        if suffix.startswith("./"):
            return prefix + "=" + resolve_argv_path(suffix, install_root, cwd)
    return resolve_argv_path(value, install_root, cwd)


def quote_argv(argv: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in argv)


def render_redirects(redirects: dict[str, str]) -> str:
    unknown = sorted(set(redirects) - set(REDIRECT_SHELL_OPS))
    if unknown:
        raise VerifyError(f"unknown redirect key(s): {', '.join(unknown)}")
    if "stdout" in redirects and "stdout-append" in redirects:
        raise VerifyError("redirects cannot contain both stdout and stdout-append")
    if "stderr" in redirects and "stderr-append" in redirects:
        raise VerifyError("redirects cannot contain both stderr and stderr-append")

    parts = []
    for key in ("stdin", "stdout", "stdout-append", "stderr", "stderr-append"):
        target = redirects.get(key)
        if target is not None:
            parts.append(f"{REDIRECT_SHELL_OPS[key]} {shlex.quote(target)}")
    return " ".join(parts)


def build_shell_command(entry: dict[str, Any], install_root: Path) -> str:
    argv = entry.get("argv")
    if not isinstance(argv, list) or not argv:
        raise VerifyError("verify command must contain a non-empty argv list")

    raw_cwd = entry.get("cwd")
    if not isinstance(raw_cwd, str):
        raise VerifyError("verify command must contain a cwd string")

    raw_redirects = entry.get("redirects", {})
    if not isinstance(raw_redirects, dict):
        raise VerifyError("verify command redirects must be an object")

    resolved_cwd = resolve_install_path(raw_cwd, install_root)
    resolved_argv = [str(resolve_install_path(str(argv[0]), install_root))] + [
        resolve_argv_value(str(arg), install_root, resolved_cwd) for arg in argv[1:]
    ]
    resolved_redirects = {
        key: str(resolve_install_path(str(target), install_root))
        for key, target in raw_redirects.items()
    }

    command = " ".join(
        part
        for part in (quote_argv(resolved_argv), render_redirects(resolved_redirects))
        if part
    )
    return f"( cd {shlex.quote(str(resolved_cwd))} && {command} )"


def entry_label(entry: dict[str, Any]) -> str:
    return (
        f"{entry.get('suite', '')} {entry.get('benchmark', '')} "
        f"verify command {entry.get('command_index', '')}"
    )


def run_directories(
    commands: list[dict[str, Any]], install_root: Path
) -> OrderedDict[Path, str]:
    directories: OrderedDict[Path, str] = OrderedDict()
    for command in commands:
        cwd = command.get("cwd")
        if not isinstance(cwd, str):
            raise VerifyError("each command must contain a cwd string")
        label = f"{command.get('suite', '')} {command.get('benchmark', '')}"
        directories.setdefault(resolve_install_path(cwd, install_root), label)
    return directories


def specinvoke_command(run_dir: Path) -> list[str]:
    return [
        "specinvoke",
        "-d",
        str(run_dir),
        "-f",
        "compare.cmd",
        "-E",
        "-e",
        "compare.err",
        "-o",
        "compare.stdout",
    ]


def verify_run_dirs(run_dirs: OrderedDict[Path, str], dry_run: bool) -> int:
    failures = 0
    total = len(run_dirs)

    for index, (run_dir, label) in enumerate(run_dirs.items(), start=1):
        command = specinvoke_command(run_dir)
        print(f"[{index}/{total}] compare: {label} ({run_dir})", flush=True)

        if dry_run:
            print(" ".join(command))
            continue

        if not (run_dir / "compare.cmd").is_file():
            failures += 1
            print(
                f"compare failed [missing compare.cmd]: {label} ({run_dir})",
                file=sys.stderr,
                flush=True,
            )
            continue

        completed = subprocess.run(command)
        if completed.returncode == 0:
            print(f"compare ok: {label}", flush=True)
        else:
            failures += 1
            print(
                f"compare failed [{completed.returncode}]: {label} ({run_dir})",
                file=sys.stderr,
                flush=True,
            )

    print(f"compare summary: total={total} failed={failures}", flush=True)
    return 1 if failures else 0


def verify_entries(
    entries: list[dict[str, Any]], install_root: Path, dry_run: bool
) -> int:
    failures = 0
    total = len(entries)

    for index, entry in enumerate(entries, start=1):
        label = entry_label(entry)
        shell_command = build_shell_command(entry, install_root)
        print(f"[{index}/{total}] compare: {label}", flush=True)

        if dry_run:
            print(shell_command)
            continue

        completed = subprocess.run(shell_command, shell=True)
        if completed.returncode == 0:
            print(f"compare ok: {label}", flush=True)
        else:
            failures += 1
            print(
                f"compare failed [{completed.returncode}]: {label}",
                file=sys.stderr,
                flush=True,
            )

    print(f"compare summary: total={total} failed={failures}", flush=True)
    return 1 if failures else 0


def main() -> int:
    try:
        args = parse_args()
        install_root = resolve_cli_path(args.install_root)
        commands_file = resolve_cli_path(args.commands_file)
        manifest = load_manifest(commands_file)
        verify_commands = manifest.get("verify_commands")
        if isinstance(verify_commands, list) and verify_commands:
            return verify_entries(
                filter_entries(verify_commands, args), install_root, args.dry_run
            )

        commands = filter_entries(manifest["commands"], args)
        return verify_run_dirs(run_directories(commands, install_root), args.dry_run)
    except VerifyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
