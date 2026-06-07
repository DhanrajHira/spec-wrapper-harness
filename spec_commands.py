"""Shared helpers for the SPEC command-capture scripts.

This module holds the logic that the generator, wrapper runner, and verifier
all need so the three scripts cannot drift apart. The most important piece is
path resolution: manifest paths are stored install-root-relative (``./...``),
and there are two distinct ways to turn them back into runnable paths.

``resolve_install_path`` absolutizes a path. It is used only for things the
shell/OS consumes directly -- the executable, the working directory, and
redirect targets -- where an absolute path is always correct and the string
never leaks into a benchmark's compared output.

``resolve_argv_value`` / ``cwd_relative_if_inside`` are used for benchmark
*arguments*. An argument that points inside the run directory is rendered
relative to that directory, reproducing the bare filename the SPEC harness
originally passed. This is deliberate: some benchmarks echo an argument into
their output (e.g. ``502.gcc_r`` writes the input source name into the
generated assembly) or use it as a name/stem to derive other files, and would
mismatch the reference output if handed an absolute path. Arguments are only
absolutized when they point outside the run directory.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Parse side: shell redirection operators -> manifest redirect keys.
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
REDIRECT_OPERATORS = frozenset(REDIRECT_KEYS)

# Render side: manifest redirect keys -> a single shell operator, in the order
# they should appear on a command line.
REDIRECT_SHELL_OPS = {
    "stdin": "<",
    "stdout": ">",
    "stdout-append": ">>",
    "stderr": "2>",
    "stderr-append": "2>>",
}
REDIRECT_ORDER = ("stdin", "stdout", "stdout-append", "stderr", "stderr-append")


class UserError(Exception):
    """A user-facing error reported without a traceback."""


def run_cli(main_impl: Callable[[], int]) -> int:
    """Run a script's main implementation with shared error handling."""
    try:
        return main_impl()
    except UserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def resolve_cli_path(value: str) -> Path:
    """Resolve a user-supplied path relative to the current directory."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def resolve_install_path(value: str, install_root: Path) -> str:
    """Absolutize an install-root-relative path for the exe, cwd, or redirects."""
    if value.startswith("./"):
        return str((install_root / value[2:]).resolve(strict=False))
    if os.path.isabs(value):
        return value
    return str((install_root / value).resolve(strict=False))


def cwd_relative_if_inside(path: Path, cwd: Path) -> str:
    """Render ``path`` relative to ``cwd`` when it lives there, else absolute."""
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
    """Resolve a benchmark argument, keeping in-cwd paths relative to the run dir."""
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
    """Render redirect targets as a shell-quoted redirection suffix."""
    unknown = sorted(set(redirects) - set(REDIRECT_SHELL_OPS))
    if unknown:
        raise UserError(f"unknown redirect key(s): {', '.join(unknown)}")
    if "stdout" in redirects and "stdout-append" in redirects:
        raise UserError("redirects cannot contain both stdout and stdout-append")
    if "stderr" in redirects and "stderr-append" in redirects:
        raise UserError("redirects cannot contain both stderr and stderr-append")

    parts = []
    for key in REDIRECT_ORDER:
        target = redirects.get(key)
        if target is not None:
            parts.append(f"{REDIRECT_SHELL_OPS[key]} {shlex.quote(target)}")
    return " ".join(parts)


def load_manifest(commands_file: Path) -> dict[str, Any]:
    """Load and minimally validate a captured-commands manifest."""
    try:
        data = json.loads(commands_file.read_text())
    except OSError as exc:
        raise UserError(f"failed to read commands file {commands_file}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise UserError(
            f"failed to parse commands file {commands_file}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise UserError("commands file must contain a top-level object")
    if not isinstance(data.get("commands"), list):
        raise UserError("commands file must contain a top-level 'commands' list")
    return data


def filter_commands(
    entries: list[dict[str, Any]],
    *,
    suites: list[str] | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter command entries by suite and case-insensitive benchmark substring."""
    suite_set = set(suites or [])
    include_filters = [value.casefold() for value in (include or [])]
    exclude_filters = [value.casefold() for value in (exclude or [])]
    filtered = []

    for command in entries:
        if suite_set and command.get("suite") not in suite_set:
            continue
        benchmark = str(command.get("benchmark", "")).casefold()
        if include_filters and not any(value in benchmark for value in include_filters):
            continue
        if exclude_filters and any(value in benchmark for value in exclude_filters):
            continue
        filtered.append(command)

    if not filtered:
        raise UserError("no commands matched the requested filters")
    return filtered


@dataclass(frozen=True)
class ResolvedCommand:
    """A command entry with its cwd, argv, and redirect targets resolved."""

    cwd: str
    argv: list[str]
    redirects: dict[str, str]


def resolve_command(
    entry: dict[str, Any], install_root: Path, context: str = "command"
) -> ResolvedCommand:
    """Validate a manifest entry and resolve its cwd, argv, and redirects.

    The executable (argv[0]), cwd, and redirect targets are absolutized; the
    remaining argv entries keep in-cwd paths relative to the run directory.
    """
    argv = entry.get("argv")
    if not isinstance(argv, list) or not argv:
        raise UserError(f"{context} must contain a non-empty argv list")

    raw_cwd = entry.get("cwd")
    if not isinstance(raw_cwd, str):
        raise UserError(f"{context} must contain a cwd string")

    raw_redirects = entry.get("redirects", {})
    if not isinstance(raw_redirects, dict):
        raise UserError(f"{context} redirects must be an object")

    resolved_cwd = resolve_install_path(raw_cwd, install_root)
    cwd_path = Path(resolved_cwd)
    resolved_argv = [resolve_install_path(str(argv[0]), install_root)] + [
        resolve_argv_value(str(arg), install_root, cwd_path) for arg in argv[1:]
    ]
    resolved_redirects = {
        key: resolve_install_path(str(target), install_root)
        for key, target in raw_redirects.items()
    }
    return ResolvedCommand(resolved_cwd, resolved_argv, resolved_redirects)
