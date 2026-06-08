#!/usr/bin/env python3
"""Compare captured SPEC run-directory outputs without rebuilding or rerunning."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any

from spec_commands import (
    UserError as VerifyError,
    filter_commands,
    load_manifest,
    quote_argv,
    render_redirects,
    resolve_cli_path,
    resolve_command,
    resolve_install_path,
    run_cli,
)


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


def build_shell_command(entry: dict[str, Any], install_root: Path) -> str:
    resolved = resolve_command(entry, install_root, "verify command")
    command = " ".join(
        part
        for part in (quote_argv(resolved.argv), render_redirects(resolved.redirects))
        if part
    )
    return f"( cd {shlex.quote(resolved.cwd)} && {command} )"


def entry_label(entry: dict[str, Any]) -> str:
    return (
        f"{entry.get('suite', '')} {entry.get('benchmark', '')} "
        f"verify command {entry.get('command_index', '')}"
    )


def benchmark_name(entry: dict[str, Any]) -> str:
    benchmark = str(entry.get("benchmark", "")).strip()
    return benchmark or entry_label(entry)


def command_number(entry: dict[str, Any]) -> str:
    return str(entry.get("command_index", "")).strip() or "unknown"


def print_summary(total: int, failures: int, failed_commands: list[str]) -> None:
    print(f"compare summary: total={total} failed={failures}", flush=True)
    for failed_command in failed_commands:
        print(f"failed benchmark: {failed_command}", flush=True)


def run_directories(
    commands: list[dict[str, Any]], install_root: Path
) -> OrderedDict[Path, tuple[str, str, str]]:
    directories: OrderedDict[Path, tuple[str, str, str]] = OrderedDict()
    for command in commands:
        cwd = command.get("cwd")
        if not isinstance(cwd, str):
            raise VerifyError("each command must contain a cwd string")
        benchmark = benchmark_name(command)
        label = f"{command.get('suite', '')} {benchmark}"
        directories.setdefault(
            Path(resolve_install_path(cwd, install_root)),
            (label, benchmark, command_number(command)),
        )
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


def verify_run_dirs(
    run_dirs: OrderedDict[Path, tuple[str, str, str]], dry_run: bool
) -> int:
    failures = 0
    failed_commands: list[str] = []
    total = len(run_dirs)

    for index, (run_dir, (label, benchmark, command_index)) in enumerate(
        run_dirs.items(), start=1
    ):
        command = specinvoke_command(run_dir)
        print(f"[{index}/{total}] compare: {label} ({run_dir})", flush=True)

        if dry_run:
            print(" ".join(command))
            continue

        if not (run_dir / "compare.cmd").is_file():
            failures += 1
            failed_commands.append(f"{benchmark} command {command_index}")
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
            failed_commands.append(f"{benchmark} command {command_index}")
            print(
                f"compare failed [{completed.returncode}]: {label} ({run_dir})",
                file=sys.stderr,
                flush=True,
            )

    print_summary(total, failures, failed_commands)
    return 1 if failures else 0


def verify_entries(
    entries: list[dict[str, Any]], install_root: Path, dry_run: bool
) -> int:
    failures = 0
    failed_commands: list[str] = []
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
            failed_commands.append(
                f"{benchmark_name(entry)} command {command_number(entry)}"
            )
            print(
                f"compare failed [{completed.returncode}]: {label}",
                file=sys.stderr,
                flush=True,
            )

    print_summary(total, failures, failed_commands)
    return 1 if failures else 0


def main() -> int:
    args = parse_args()
    install_root = resolve_cli_path(args.install_root)
    commands_file = resolve_cli_path(args.commands_file)
    manifest = load_manifest(commands_file)

    def selected(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return filter_commands(
            entries,
            suites=args.suite,
            include=args.benchmark,
            exclude=args.skip_benchmark,
        )

    verify_commands = manifest.get("verify_commands")
    if isinstance(verify_commands, list) and verify_commands:
        return verify_entries(selected(verify_commands), install_root, args.dry_run)

    commands = selected(manifest["commands"])
    return verify_run_dirs(run_directories(commands, install_root), args.dry_run)


if __name__ == "__main__":
    raise SystemExit(run_cli(main))
