# SPEC Commands Manifest

`spec_integer_run_commands.json` contains the captured SPEC CPU2017 integer reference workload commands generated from fake harness runs. It includes only benchmark invocation commands, not build commands, PGO training commands, input-generation commands, or compare/specdiff commands.

## Top-Level Structure

```json
{
  "generated_from": ["./..."],
  "setup_required": ["..."],
  "commands": [{ "...": "..." }]
}
```

- `generated_from`: source fake-run logs used to produce the manifest.
- `setup_required`: `runcpu --action=setup` commands that must be run before replaying the manifest. This materializes run-directory executable links and generated inputs such as `BuckBunny.yuv`.
- `commands`: ordered list of benchmark commands to run.

## Command Entries

Each object in `commands` has this structure:

```json
{
  "suite": "intrate",
  "benchmark": "500.perlbench_r",
  "phase": "benchmark",
  "command_index": 1,
  "cwd": "./install-root-relative/run/directory",
  "command": "./install-root-relative/executable ... > ./install-root-relative/output 2>> ./install-root-relative/error",
  "argv": ["./install-root-relative/executable", "arg1", "arg2"],
  "redirects": {
    "stdout": "./install-root-relative/output",
    "stderr-append": "./install-root-relative/error"
  }
}
```

- `suite`: SPEC integer suite, currently `intspeed` or `intrate`.
- `benchmark`: SPEC benchmark name, such as `525.x264_r`.
- `phase`: command phase. The manifest contains only `benchmark` entries.
- `command_index`: one-based order of the command within that benchmark.
- `cwd`: working directory that the SPEC harness uses for this invocation.
- `command`: shell command string with normalized install-root-relative paths and shell redirections.
- `argv`: executable argument vector only. This excludes shell redirections.
- `redirects`: parsed shell redirections captured separately from `argv`.

Redirect keys use explicit stream/action names:

- `stdin`: read stdin from file, equivalent to `<`.
- `stdout`: write stdout to file, equivalent to `>` or `1>`.
- `stdout-append`: append stdout to file, equivalent to `>>` or `1>>`.
- `stderr`: write stderr to file, equivalent to `2>`.
- `stderr-append`: append stderr to file, equivalent to `2>>`.

## Path Normalization

The manifest normalizes executable paths and file-like command-line arguments to paths relative to the SPEC install root. Redirection targets are also install-root-relative paths.

This matches the SPEC harness command layout after `runcpu --action=setup` has been run. The setup step creates the run-directory executable links and performs benchmark-specific setup such as `x264` input generation.

Consumers should resolve `cwd`, file-like `argv` entries, and redirect target values against the install root before executing commands from another working directory. The `command` field can be run directly by a shell from the install root.

## Execution Order

The `commands` array is ordered exactly as the SPEC harness emitted the benchmark invocations. Consumers should execute entries in this order unless they deliberately implement SPEC-compatible scheduling.

Some benchmarks have multiple inputs or dependent stages. Entries for the same `benchmark` must run sequentially by ascending `command_index`; do not parallelize commands within a single benchmark.

Examples of multi-command benchmarks include:

- `500.perlbench_r` / `600.perlbench_s`: separate Perl workloads.
- `502.gcc_r` / `602.gcc_s`: separate compiler input/option combinations.
- `525.x264_r` / `625.x264_s`: pass 1, pass 2, then seek/dumpyuv stage; later stages depend on files produced by earlier stages.
- `557.xz_r` / `657.xz_s`: separate compressed inputs.

`525.x264_r` and `625.x264_s` also require setup-generated `BuckBunny.yuv` inputs. These are produced by the required `runcpu --action=setup` step, not by entries in `commands`.

## Replaying Commands

To replay a command from JSON:

1. Run the listed `setup_required` commands first if the run directories have not already been set up.
2. Run `commands` in manifest order.
3. For each entry, resolve `cwd` against the install root and change to that directory.
4. Execute `argv` as the process argument vector.
5. Apply each `redirects` entry as the equivalent shell redirection.

The `command` field can be used directly by a shell from the install root if preferred, but `argv` plus `redirects` is safer for programmatic runners.

## Wrapper Runner

`run_spec_with_wrapper.py` runs the captured benchmark commands through a user-provided wrapper command. It resolves install-root-relative paths to absolute paths, reconstructs each benchmark command from `argv` plus `redirects`, and executes each wrapper invocation in a subshell rooted at the captured benchmark working directory.

Example Intel Pin-style usage:

```bash
./run_command_capture/run_spec_with_wrapper.py \
  --install-root /path/to/spec-install \
  --jobs 8 \
  -- /path/to/pin -t /path/to/custom-tool {benchmark_cmd}
```

`--commands-file` is optional. If omitted, it defaults to `spec_integer_run_commands.json` next to `run_spec_with_wrapper.py`.

Each command is rendered as:

```bash
( cd <resolved-cwd> && <wrapper with substitutions> )
```

The main wrapper placeholder is `{benchmark_cmd}`. It expands to the full shell-quoted benchmark executable, arguments, and file redirects.

Other command placeholders are:

- `{benchmark_argv}`: benchmark executable plus arguments, without redirects.
- `{benchmark_args}`: benchmark arguments only, without executable or redirects.
- `{benchmark_command}`: alias for `{benchmark_argv}`.

Scalar placeholders are:

- `{install_root}`
- `{suite}`
- `{benchmark}`
- `{benchmark_name}`
- `{phase}`
- `{command_index}`
- `{cwd}`
- `{benchmark_cwd}`
- `{benchmark_exe}`
- `{stdin}`
- `{stdout}`
- `{stdout_append}` or `{stdout-append}`
- `{stderr}`
- `{stderr_append}` or `{stderr-append}`

Command placeholders must be standalone wrapper arguments because they expand to shell command fragments. Scalar placeholders may be embedded inside larger wrapper arguments, such as `--name={benchmark_name}`.

`--jobs N` runs benchmark groups in parallel. `--jobs -1` uses the maximum allowed parallelism, which is one worker per benchmark group after filtering. Commands for the same `(suite, benchmark)` pair are always kept sequential and in manifest order.

The runner does not run `setup_required` commands. Run those first if the run directories have not already been materialized.
