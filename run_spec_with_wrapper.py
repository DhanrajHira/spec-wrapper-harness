#!/usr/bin/env python3
"""Run captured SPEC CPU2017 commands through a user-provided wrapper."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_-]+)\}")
PLACEHOLDER_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")

COMMAND_PLACEHOLDERS = {
    "benchmark_args",
    "benchmark_argv",
    "benchmark_cmd",
    "benchmark_command",
}

RUN_COMMAND_PLACEHOLDERS = {
    "benchmark_argv",
    "benchmark_cmd",
    "benchmark_command",
}

SCALAR_PLACEHOLDERS = {
    "benchmark",
    "benchmark_cwd",
    "benchmark_exe",
    "benchmark_name",
    "command_idx",
    "command_index",
    "cwd",
    "install_root",
    "phase",
    "stderr",
    "stderr-append",
    "stderr_append",
    "stdin",
    "stdout",
    "stdout-append",
    "stdout_append",
    "suite",
}

KNOWN_PLACEHOLDERS = COMMAND_PLACEHOLDERS | SCALAR_PLACEHOLDERS

REDIRECT_SHELL_OPS = {
    "stdin": "<",
    "stdout": ">",
    "stdout-append": ">>",
    "stderr": "2>",
    "stderr-append": "2>>",
}


class RunnerError(Exception):
    """A user-facing runner error."""


@dataclass(frozen=True)
class RenderedCommand:
    manifest_index: int
    total_commands: int
    suite: str
    benchmark: str
    command_index: Any
    shell_command: str

    @property
    def group_key(self) -> tuple[str, str]:
        return (self.suite, self.benchmark)

    @property
    def label(self) -> str:
        return f"{self.suite} {self.benchmark} command {self.command_index}"


def jobs_count(value: str) -> int:
    parsed = int(value)
    if parsed != -1 and parsed < 1:
        raise argparse.ArgumentTypeError("must be -1 or >= 1")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run captured SPEC commands through a wrapper command.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--install-root", required=True, help="SPEC install root")
    parser.add_argument(
        "--commands-file",
        help="captured commands JSON; defaults to spec_integer_run_commands.json next to this script",
    )
    parser.add_argument(
        "--jobs",
        type=jobs_count,
        default=1,
        help="parallel commands; use -1 for maximum allowed parallelism",
    )
    parser.add_argument(
        "--serialize-benchmark-commands",
        action="store_true",
        help="keep commands for the same suite/benchmark sequential",
    )
    parser.add_argument(
        "--suite", action="append", help="suite filter; may be repeated"
    )
    parser.add_argument(
        "--benchmark", action="append", help="benchmark filter; may be repeated"
    )
    parser.add_argument(
        "--skip-benchmark",
        action="append",
        help="benchmark exclusion filter; case-insensitive substring match; may be repeated",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print commands without running them"
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="continue scheduling after command failures",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="print shell commands before execution"
    )
    parser.add_argument(
        "--placeholder",
        action="append",
        default=[],
        nargs=2,
        metavar=("NAME", "COMMAND"),
        help="define custom scalar placeholder NAME from stdout of COMMAND; may be repeated",
    )
    parser.add_argument(
        "wrapper",
        nargs=argparse.REMAINDER,
        help="wrapper command after --; usually includes a benchmark command placeholder",
    )
    args = parser.parse_args()

    if args.wrapper and args.wrapper[0] == "--":
        args.wrapper = args.wrapper[1:]

    if not args.wrapper:
        parser.error("wrapper command is required after --")

    args.placeholder = parse_custom_placeholders(args.placeholder)
    validate_custom_placeholder_templates(args.placeholder)
    validate_wrapper(args.wrapper, set(args.placeholder))
    return args


def parse_custom_placeholders(
    specs: list[tuple[str, str]],
) -> OrderedDict[str, str]:
    placeholders: OrderedDict[str, str] = OrderedDict()
    for name, command in specs:
        if not PLACEHOLDER_NAME_RE.fullmatch(name):
            raise RunnerError(
                f"invalid custom placeholder name {name!r}; use letters, digits, '_' or '-'"
            )
        if name in KNOWN_PLACEHOLDERS:
            raise RunnerError(
                f"custom placeholder {{{name}}} conflicts with a built-in"
            )
        if name in placeholders:
            raise RunnerError(
                f"custom placeholder {{{name}}} is defined more than once"
            )
        if not command.strip():
            raise RunnerError(f"custom placeholder {{{name}}} command is empty")
        placeholders[name] = command
    return placeholders


def validate_custom_placeholder_templates(
    placeholders: OrderedDict[str, str],
) -> None:
    available = set(SCALAR_PLACEHOLDERS)
    for name, command in placeholders.items():
        used = set(PLACEHOLDER_RE.findall(command))
        command_placeholders = sorted(used & COMMAND_PLACEHOLDERS)
        if command_placeholders:
            formatted = ", ".join(f"{{{item}}}" for item in command_placeholders)
            raise RunnerError(
                f"custom placeholder {{{name}}} command cannot use command placeholder(s): {formatted}"
            )

        unknown = sorted(used - available)
        if unknown:
            formatted = ", ".join(f"{{{item}}}" for item in unknown)
            raise RunnerError(
                f"custom placeholder {{{name}}} command uses unknown placeholder(s): {formatted}"
            )
        available.add(name)


def validate_wrapper(wrapper: list[str], custom_placeholders: set[str]) -> None:
    placeholders: set[str] = set()
    for token in wrapper:
        placeholders.update(PLACEHOLDER_RE.findall(token))

    unknown = sorted(placeholders - KNOWN_PLACEHOLDERS - custom_placeholders)
    if unknown:
        raise RunnerError(f"unknown placeholder(s): {', '.join(unknown)}")

    if not placeholders & RUN_COMMAND_PLACEHOLDERS:
        required = ", ".join(f"{{{name}}}" for name in sorted(RUN_COMMAND_PLACEHOLDERS))
        print(
            f"warning: wrapper does not include one of: {required}",
            file=sys.stderr,
        )


def load_commands(commands_file: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(commands_file.read_text())
    except OSError as exc:
        raise RunnerError(
            f"failed to read commands file {commands_file}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RunnerError(
            f"failed to parse commands file {commands_file}: {exc}"
        ) from exc

    commands = data.get("commands")
    if not isinstance(commands, list):
        raise RunnerError("commands file must contain a top-level 'commands' list")
    return commands


def resolve_commands_file(commands_file: str | None) -> Path:
    if commands_file:
        return Path(commands_file).expanduser().resolve(strict=False)
    return (
        Path(__file__).with_name("spec_integer_run_commands.json").resolve(strict=False)
    )


def filter_commands(
    commands: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    suites = set(args.suite or [])
    benchmark_filters = [value.casefold() for value in args.benchmark or []]
    skip_benchmark_filters = [
        value.casefold() for value in args.skip_benchmark or []
    ]
    filtered = []

    for command in commands:
        if suites and command.get("suite") not in suites:
            continue
        benchmark = str(command.get("benchmark", "")).casefold()
        if benchmark_filters and not any(
            value in benchmark for value in benchmark_filters
        ):
            continue
        if skip_benchmark_filters and any(
            value in benchmark for value in skip_benchmark_filters
        ):
            continue
        filtered.append(command)

    if not filtered:
        raise RunnerError("no commands matched the requested filters")
    return filtered


def resolve_install_path(value: str, install_root: Path) -> str:
    if value.startswith("./"):
        return str((install_root / value[2:]).resolve(strict=False))
    if os.path.isabs(value):
        return value
    return value


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
        raise RunnerError(f"unknown redirect key(s): {', '.join(unknown)}")
    if "stdout" in redirects and "stdout-append" in redirects:
        raise RunnerError("redirects cannot contain both stdout and stdout-append")
    if "stderr" in redirects and "stderr-append" in redirects:
        raise RunnerError("redirects cannot contain both stderr and stderr-append")

    parts = []
    for key in ("stdin", "stdout", "stdout-append", "stderr", "stderr-append"):
        target = redirects.get(key)
        if target is not None:
            parts.append(f"{REDIRECT_SHELL_OPS[key]} {shlex.quote(target)}")
    return " ".join(parts)


def redirect_placeholder_values(redirects: dict[str, str]) -> dict[str, str | None]:
    return {
        "stdin": redirects.get("stdin"),
        "stdout": redirects.get("stdout", "/dev/null"),
        "stdout-append": redirects.get("stdout-append", "/dev/null"),
        "stdout_append": redirects.get("stdout-append", "/dev/null"),
        "stderr": redirects.get("stderr", "/dev/null"),
        "stderr-append": redirects.get("stderr-append", "/dev/null"),
        "stderr_append": redirects.get("stderr-append", "/dev/null"),
    }


def render_scalar_template(template: str, scalars: dict[str, str | None]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in scalars:
            raise RunnerError(f"unknown placeholder: {{{name}}}")
        value = scalars[name]
        if value is None:
            raise RunnerError(
                f"placeholder {{{name}}} is not available for this command"
            )
        return shlex.quote(value)

    return PLACEHOLDER_RE.sub(replace, template)


def evaluate_custom_placeholders(
    placeholders: OrderedDict[str, str],
    scalars: dict[str, str | None],
    install_root: Path,
    label: str,
) -> dict[str, str]:
    values: dict[str, str] = {}
    available = dict(scalars)

    for name, template in placeholders.items():
        rendered = render_scalar_template(template, available)
        completed = subprocess.run(
            rendered,
            cwd=install_root,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.rstrip() or rendered
            raise RunnerError(
                f"custom placeholder {{{name}}} failed for {label} "
                f"with exit code {completed.returncode}: {detail}"
            )
        value = completed.stdout.rstrip("\n")
        values[name] = value
        available[name] = value

    return values


def build_shell_command(
    command: dict[str, Any],
    wrapper: list[str],
    custom_placeholders: OrderedDict[str, str],
    install_root: Path,
    manifest_index: int,
    total_commands: int,
) -> RenderedCommand:
    argv = command.get("argv")
    if not isinstance(argv, list) or not argv:
        raise RunnerError(
            f"command {manifest_index} must contain a non-empty argv list"
        )

    raw_cwd = command.get("cwd")
    if not isinstance(raw_cwd, str):
        raise RunnerError(f"command {manifest_index} must contain a cwd string")

    raw_redirects = command.get("redirects", {})
    if not isinstance(raw_redirects, dict):
        raise RunnerError(f"command {manifest_index} redirects must be an object")

    resolved_cwd = resolve_install_path(raw_cwd, install_root)
    resolved_cwd_path = Path(resolved_cwd)
    resolved_argv = [resolve_install_path(str(argv[0]), install_root)] + [
        resolve_argv_value(str(arg), install_root, resolved_cwd_path)
        for arg in argv[1:]
    ]
    resolved_redirects = {
        key: resolve_install_path(str(target), install_root)
        for key, target in raw_redirects.items()
    }

    redirect_shell = render_redirects(resolved_redirects)
    benchmark_argv_shell = quote_argv(resolved_argv)
    benchmark_cmd = " ".join(
        part for part in (benchmark_argv_shell, redirect_shell) if part
    )

    suite = str(command.get("suite", ""))
    benchmark = str(command.get("benchmark", ""))
    phase = str(command.get("phase", ""))
    command_index = command.get("command_index", manifest_index)

    scalars: dict[str, str | None] = {
        "benchmark": benchmark,
        "benchmark_cwd": resolved_cwd,
        "benchmark_exe": resolved_argv[0],
        "benchmark_name": benchmark,
        "command_idx": str(command_index),
        "command_index": str(command_index),
        "cwd": resolved_cwd,
        "install_root": str(install_root),
        "phase": phase,
        "suite": suite,
    }
    scalars.update(redirect_placeholder_values(resolved_redirects))

    scalars.update(
        evaluate_custom_placeholders(
            custom_placeholders,
            scalars,
            install_root,
            f"{suite} {benchmark} command {command_index}",
        )
    )

    snippets = {
        "benchmark_args": quote_argv(resolved_argv[1:]),
        "benchmark_argv": benchmark_argv_shell,
        "benchmark_cmd": benchmark_cmd,
        "benchmark_command": benchmark_argv_shell,
    }

    wrapper_parts = [
        rendered
        for rendered in (
            render_wrapper_token(token, scalars, snippets) for token in wrapper
        )
        if rendered
    ]
    if not wrapper_parts:
        raise RunnerError("wrapper rendered to an empty command")

    wrapper_shell = " ".join(wrapper_parts)
    shell_command = f"( cd {shlex.quote(resolved_cwd)} && {wrapper_shell} )"

    return RenderedCommand(
        manifest_index=manifest_index,
        total_commands=total_commands,
        suite=suite,
        benchmark=benchmark,
        command_index=command_index,
        shell_command=shell_command,
    )


def render_wrapper_token(
    token: str,
    scalars: dict[str, str | None],
    snippets: dict[str, str],
) -> str:
    exact = PLACEHOLDER_RE.fullmatch(token)
    if exact and exact.group(1) in snippets:
        return snippets[exact.group(1)]

    embedded_command_placeholders = COMMAND_PLACEHOLDERS & set(
        PLACEHOLDER_RE.findall(token)
    )
    if embedded_command_placeholders:
        formatted = ", ".join(
            f"{{{name}}}" for name in sorted(embedded_command_placeholders)
        )
        raise RunnerError(
            f"command placeholder(s) must be standalone wrapper arguments: {formatted}"
        )

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in scalars:
            raise RunnerError(f"unknown placeholder: {{{name}}}")
        value = scalars[name]
        if value is None:
            raise RunnerError(
                f"placeholder {{{name}}} is not available for this command"
            )
        return value

    return shlex.quote(PLACEHOLDER_RE.sub(replace, token))


def render_commands(
    commands: list[dict[str, Any]],
    wrapper: list[str],
    custom_placeholders: OrderedDict[str, str],
    install_root: Path,
) -> list[RenderedCommand]:
    total_commands = len(commands)
    return [
        build_shell_command(
            command,
            wrapper,
            custom_placeholders,
            install_root,
            index,
            total_commands,
        )
        for index, command in enumerate(commands, start=1)
    ]


def print_dry_run(rendered_commands: list[RenderedCommand]) -> int:
    for command in rendered_commands:
        print(command.shell_command)
    return 0


def group_commands(
    rendered_commands: list[RenderedCommand],
) -> deque[list[RenderedCommand]]:
    groups: OrderedDict[tuple[str, str], list[RenderedCommand]] = OrderedDict()
    for command in rendered_commands:
        groups.setdefault(command.group_key, []).append(command)
    return deque(groups.values())


def run_rendered_commands(
    rendered_commands: list[RenderedCommand],
    jobs: int,
    continue_on_error: bool,
    verbose: bool,
    serialize_benchmark_commands: bool,
) -> int:
    work_items = (
        group_commands(rendered_commands)
        if serialize_benchmark_commands
        else deque([command] for command in rendered_commands)
    )
    max_workers = len(work_items) if jobs == -1 else min(jobs, len(work_items))
    queue_lock = threading.Lock()
    print_lock = threading.Lock()
    stop_event = threading.Event()

    def next_group() -> list[RenderedCommand] | None:
        with queue_lock:
            if stop_event.is_set() and not continue_on_error:
                return None
            if not work_items:
                return None
            return work_items.popleft()

    def worker() -> list[tuple[RenderedCommand, int]]:
        failures = []
        while True:
            group = next_group()
            if group is None:
                return failures

            for command in group:
                if stop_event.is_set() and not continue_on_error:
                    break

                with print_lock:
                    print(
                        f"[{command.manifest_index}/{command.total_commands}] {command.label}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if verbose:
                        print(command.shell_command, file=sys.stderr, flush=True)

                started = time.monotonic()
                completed = subprocess.run(command.shell_command, shell=True)
                elapsed = time.monotonic() - started
                if completed.returncode != 0:
                    failures.append((command, completed.returncode))
                    with print_lock:
                        print(
                            f"failed [{completed.returncode}]: {command.label}",
                            file=sys.stderr,
                            flush=True,
                        )
                    if not continue_on_error:
                        stop_event.set()
                        break
                else:
                    with print_lock:
                        print(
                            f"completed [0]: {command.label} ({elapsed:.1f}s)",
                            file=sys.stderr,
                            flush=True,
                        )

    failures: list[tuple[RenderedCommand, int]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(worker) for _ in range(max_workers)]
        for future in concurrent.futures.as_completed(futures):
            failures.extend(future.result())

    if not failures:
        return 0

    first_return_code = failures[0][1]
    return first_return_code if first_return_code else 1


def main() -> int:
    try:
        args = parse_args()
        install_root = Path(args.install_root).expanduser().resolve(strict=False)
        if not install_root.is_dir():
            raise RunnerError(f"install root is not a directory: {install_root}")

        commands_file = resolve_commands_file(args.commands_file)
        commands = filter_commands(load_commands(commands_file), args)
        rendered_commands = render_commands(
            commands,
            args.wrapper,
            args.placeholder,
            install_root,
        )

        if args.dry_run:
            return print_dry_run(rendered_commands)

        return run_rendered_commands(
            rendered_commands,
            jobs=args.jobs,
            continue_on_error=args.continue_on_error,
            verbose=args.verbose,
            serialize_benchmark_commands=args.serialize_benchmark_commands,
        )
    except RunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
