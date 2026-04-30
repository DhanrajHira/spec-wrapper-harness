#!/usr/bin/env python3
"""Generate a concrete SPEC command JSON manifest for a selected config."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_SUITES = ("intspeed", "intrate", "fpspeed", "fprate")
RUNNING_RE = re.compile(r"^\s+Running\s+(\S+)\s+")
REDIRECT_KEYS = {
    "<": "stdin",
    "0<": "stdin",
    ">": "stdout",
    "1>": "stdout",
    ">>": "stdout-append",
    "1>>": "stdout-append",
    "2>": "stderr",
    "2>>": "stderr-append",
}
REDIRECT_OPERATORS = set(REDIRECT_KEYS)
FILE_VALUE_OPTIONS = {
    "-o",
    "--output",
    "--stats",
}


class GeneratorError(Exception):
    """A user-facing generator error."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build/setup SPEC and generate a benchmark command JSON manifest.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config", required=True, help="SPEC config name/path to pass to runcpu"
    )
    parser.add_argument(
        "--suite-config",
        action="append",
        default=[],
        metavar="SUITE=CONFIG",
        help="per-suite config override; may be repeated",
    )
    parser.add_argument(
        "--suite",
        action="append",
        choices=DEFAULT_SUITES,
        help="suite to include; may be repeated",
    )
    parser.add_argument("--size", default="ref", help="SPEC workload size")
    parser.add_argument("--tune", default="peak", help="SPEC tune setting")
    parser.add_argument(
        "--iterations", type=positive_int, default=1, help="fake run iterations"
    )
    parser.add_argument(
        "--build-ncpus",
        type=positive_int,
        default=detect_build_ncpus(),
        help="value for --define build_ncpus=N",
    )
    parser.add_argument(
        "--output",
        default="run_command_capture/spec_integer_run_commands.json",
        help="output JSON path",
    )
    parser.add_argument(
        "--log-dir",
        default="run_command_capture",
        help="directory for captured fake-run logs",
    )
    parser.add_argument(
        "--skip-build", action="store_true", help="do not run runcpu --action=build"
    )
    parser.add_argument(
        "--skip-setup", action="store_true", help="do not run runcpu --action=setup"
    )
    parser.add_argument(
        "--fake-log",
        action="append",
        default=[],
        metavar="SUITE=PATH",
        help="parse an existing fake-run log instead of running runcpu --fake for that suite",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="continue running later suites after a runcpu failure",
    )
    args = parser.parse_args()

    args.suite = args.suite or list(DEFAULT_SUITES)
    args.suite_config = parse_mapping(args.suite_config, "--suite-config")
    args.fake_log = parse_mapping(args.fake_log, "--fake-log")
    return args


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def detect_build_ncpus() -> int:
    return os.cpu_count() or 1


def parse_mapping(values: list[str], option_name: str) -> dict[str, str]:
    parsed = {}
    for value in values:
        if "=" not in value:
            raise GeneratorError(f"{option_name} expects SUITE=VALUE, got {value!r}")
        key, item = value.split("=", 1)
        if not key or not item:
            raise GeneratorError(f"{option_name} expects SUITE=VALUE, got {value!r}")
        parsed[key] = item
    return parsed


def suite_config(args: argparse.Namespace, suite: str) -> str:
    return args.suite_config.get(suite, args.config)


def install_root() -> Path:
    return Path(__file__).resolve().parents[1]


def root_relative(path: str | Path, root: Path) -> str:
    path = Path(path)
    try:
        rel = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return str(path)
    return "./" + rel.as_posix()


def build_command(config: str, suite: str, args: argparse.Namespace) -> list[str]:
    command = [
        "./bin/runcpu",
        f"--config={config}",
        "--action=build",
        "--rebuild",
        f"--size={args.size}",
        f"--tune={args.tune}",
    ]
    command.extend(["--define", f"build_ncpus={args.build_ncpus}"])
    command.append(suite)
    return command


def clobber_command(config: str, suite: str, args: argparse.Namespace) -> list[str]:
    return [
        "./bin/runcpu",
        f"--config={config}",
        "--action=clobber",
        f"--size={args.size}",
        f"--tune={args.tune}",
        suite,
    ]


def setup_command(config: str, suite: str, args: argparse.Namespace) -> list[str]:
    return [
        "./bin/runcpu",
        f"--config={config}",
        "--action=setup",
        f"--size={args.size}",
        f"--tune={args.tune}",
        "--nobuild",
        suite,
    ]


def fake_run_command(config: str, suite: str, args: argparse.Namespace) -> list[str]:
    return [
        "./bin/runcpu",
        f"--config={config}",
        "--action=run",
        f"--size={args.size}",
        f"--tune={args.tune}",
        f"--iterations={args.iterations}",
        "--fake",
        "--nobuild",
        suite,
    ]


def setup_required_entry(config: str, suite: str, args: argparse.Namespace) -> str:
    return " ".join(shlex.quote(part) for part in setup_command(config, suite, args))


def run_runcpu(command: list[str], root: Path, log_path: Path | None = None) -> None:
    printable = " ".join(shlex.quote(part) for part in command)
    print(f"running: {printable}", file=sys.stderr, flush=True)
    if log_path is None:
        completed = subprocess.run(command, cwd=root)
    else:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w") as log:
            completed = subprocess.run(
                command, cwd=root, stdout=log, stderr=subprocess.STDOUT
            )
    if completed.returncode != 0:
        target = f"; see {log_path}" if log_path is not None else ""
        raise GeneratorError(
            f"runcpu failed with exit code {completed.returncode}{target}"
        )


def generate_logs(args: argparse.Namespace, root: Path) -> dict[str, Path]:
    logs: dict[str, Path] = {}
    log_dir = (root / args.log_dir).resolve(strict=False)

    for suite in args.suite:
        config = suite_config(args, suite)
        existing_log = args.fake_log.get(suite)

        try:
            if not args.skip_build:
                run_runcpu(clobber_command(config, suite, args), root)
                run_runcpu(build_command(config, suite, args), root)
            if not args.skip_setup:
                run_runcpu(setup_command(config, suite, args), root)

            if existing_log:
                existing_path = Path(existing_log).expanduser()
                if not existing_path.is_absolute():
                    existing_path = root / existing_path
                logs[suite] = existing_path.resolve(strict=False)
            else:
                log_path = log_dir / f"{suite}_fake_run.log"
                run_runcpu(
                    fake_run_command(config, suite, args), root, log_path=log_path
                )
                logs[suite] = log_path
        except GeneratorError:
            if not args.keep_going:
                raise
            print(f"warning: skipping suite after failure: {suite}", file=sys.stderr)

    return logs


def parse_fake_log(log_path: Path, suite: str, root: Path) -> list[dict[str, Any]]:
    try:
        lines = log_path.read_text().splitlines()
    except OSError as exc:
        raise GeneratorError(f"failed to read fake log {log_path}: {exc}") from exc

    commands: list[dict[str, Any]] = []
    in_running_phase = False
    in_benchmark_run = False
    current_benchmark: str | None = None
    current_cwd: str | None = None
    command_indexes: dict[str, int] = {}

    for line in lines:
        if line == "Running Benchmarks":
            in_running_phase = True
            continue
        if not in_running_phase:
            continue

        match = RUNNING_RE.match(line)
        if match:
            current_benchmark = match.group(1)
            current_cwd = None
            continue

        if line.startswith("%% Fake commands from benchmark_run"):
            in_benchmark_run = True
            current_cwd = None
            continue
        if line.startswith("%% End of fake output from benchmark_run"):
            in_benchmark_run = False
            current_cwd = None
            continue
        if not in_benchmark_run or current_benchmark is None:
            continue

        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("#")
            or stripped.startswith("export ")
            or stripped.startswith("unset ")
        ):
            continue
        if stripped.startswith("specinvoke exit:"):
            current_cwd = None
            continue
        if stripped.startswith("cd "):
            parts = shlex.split(stripped)
            if len(parts) != 2:
                raise GeneratorError(f"unsupported cd line in {log_path}: {line}")
            current_cwd = parts[1]
            continue
        if current_cwd is None:
            continue

        command_indexes[current_benchmark] = (
            command_indexes.get(current_benchmark, 0) + 1
        )
        commands.append(
            build_entry(
                suite=suite,
                benchmark=current_benchmark,
                command_index=command_indexes[current_benchmark],
                cwd=current_cwd,
                raw_command=stripped,
                root=root,
            )
        )
        current_cwd = None

    return commands


def build_entry(
    suite: str,
    benchmark: str,
    command_index: int,
    cwd: str,
    raw_command: str,
    root: Path,
) -> dict[str, Any]:
    argv, redirects = split_command(raw_command)
    if not argv:
        raise GeneratorError(f"empty command for {suite} {benchmark}")

    cwd_path = Path(cwd).resolve(strict=False)
    normalized_argv = normalize_argv(argv, cwd_path, root)
    normalized_redirects = {
        key: normalize_path(target, cwd_path, root) for key, target in redirects.items()
    }
    normalized_command = render_command(normalized_argv, normalized_redirects)

    return {
        "suite": suite,
        "benchmark": benchmark,
        "phase": "benchmark",
        "command_index": command_index,
        "cwd": root_relative(cwd_path, root),
        "command": normalized_command,
        "argv": normalized_argv,
        "redirects": normalized_redirects,
    }


def split_command(command: str) -> tuple[list[str], dict[str, str]]:
    tokens = shlex.split(command)
    argv: list[str] = []
    redirects: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in REDIRECT_OPERATORS:
            index += 1
            if index >= len(tokens):
                raise GeneratorError(
                    f"redirect {token!r} is missing a target in: {command}"
                )
            redirects[REDIRECT_KEYS[token]] = tokens[index]
        else:
            argv.append(token)
        index += 1
    return argv, redirects


def normalize_argv(argv: list[str], cwd: Path, root: Path) -> list[str]:
    normalized = []
    previous = ""
    for index, token in enumerate(argv):
        if index == 0:
            normalized.append(normalize_path(token, cwd, root))
        elif previous in FILE_VALUE_OPTIONS:
            normalized.append(normalize_path(token, cwd, root))
        elif token.startswith("-I") and len(token) > 2:
            normalized.append("-I" + normalize_path(token[2:], cwd, root))
        elif token.startswith("-L") and len(token) > 2:
            normalized.append("-L" + normalize_path(token[2:], cwd, root))
        elif should_normalize_value(token, cwd):
            normalized.append(normalize_path(token, cwd, root))
        else:
            normalized.append(token)
        previous = token
    return normalized


def should_normalize_value(token: str, cwd: Path) -> bool:
    if token.startswith("-"):
        return False
    if token.startswith(("/", "./", "../")):
        return True
    if "/" in token or "." in token:
        return True
    return (cwd / token).exists()


def normalize_path(token: str, cwd: Path, root: Path) -> str:
    if os.path.isabs(token):
        path = Path(token)
    else:
        path = cwd / token
    return root_relative(path, root)


def render_command(argv: list[str], redirects: dict[str, str]) -> str:
    parts = list(argv)
    if "stdin" in redirects:
        parts.extend(["<", redirects["stdin"]])
    if "stdout" in redirects:
        parts.extend([">", redirects["stdout"]])
    if "stdout-append" in redirects:
        parts.extend([">>", redirects["stdout-append"]])
    if "stderr" in redirects:
        parts.extend(["2>", redirects["stderr"]])
    if "stderr-append" in redirects:
        parts.extend(["2>>", redirects["stderr-append"]])
    return " ".join(parts)


def write_manifest(args: argparse.Namespace, root: Path, logs: dict[str, Path]) -> None:
    commands: list[dict[str, Any]] = []
    for suite in args.suite:
        log_path = logs.get(suite)
        if log_path is None:
            continue
        commands.extend(parse_fake_log(log_path, suite, root))

    if not commands:
        raise GeneratorError("no benchmark commands were captured")

    manifest = {
        "generated_from": [
            root_relative(logs[suite], root) for suite in args.suite if suite in logs
        ],
        "setup_required": [
            setup_required_entry(suite_config(args, suite), suite, args)
            for suite in args.suite
            if suite in logs
        ],
        "commands": commands,
    }

    output = (
        (root / args.output).resolve(strict=False)
        if not os.path.isabs(args.output)
        else Path(args.output)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {output} ({len(commands)} commands)", file=sys.stderr)


def main() -> int:
    try:
        args = parse_args()
        root = install_root()
        logs = generate_logs(args, root)
        write_manifest(args, root, logs)
        return 0
    except GeneratorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
