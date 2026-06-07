#!/usr/bin/env python3
"""Tests for spec_commands path resolution and command rendering.

The most important cases lock in the "arguments stay relative to the run dir"
behavior. Some benchmarks echo an argument into their compared output (e.g.
502.gcc_r writes the input source name into the generated assembly) or use an
argument as a name/stem to derive other files, so handing them an absolute path
breaks output comparison. These tests fail if a future change ever routes
benchmark arguments through the absolutizing resolver.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

import spec_commands as sc

INSTALL_ROOT = Path("/spec/install")
RUN_DIR = "./benchspec/CPU/502.gcc_r/run/run_peak.0000"
RUN_DIR_ABS = str((INSTALL_ROOT / RUN_DIR[2:]).resolve(strict=False))


class ResolveInstallPathTests(unittest.TestCase):
    def test_dot_slash_is_absolutized_against_root(self):
        result = sc.resolve_install_path("./bin/exe", INSTALL_ROOT)
        self.assertTrue(os.path.isabs(result))
        self.assertEqual(result, str((INSTALL_ROOT / "bin/exe").resolve(strict=False)))

    def test_absolute_passes_through(self):
        self.assertEqual(
            sc.resolve_install_path("/dev/null", INSTALL_ROOT), "/dev/null"
        )


class ResolveArgvValueTests(unittest.TestCase):
    """The gcc fix: in-cwd args stay relative; out-of-cwd args go absolute."""

    def setUp(self):
        self.cwd = Path(RUN_DIR_ABS)

    def test_in_cwd_arg_renders_relative_not_absolute(self):
        # 502.gcc_r input source; must stay "gcc-pp.c", never an absolute path.
        arg = f"{RUN_DIR}/gcc-pp.c"
        result = sc.resolve_argv_value(arg, INSTALL_ROOT, self.cwd)
        self.assertEqual(result, "gcc-pp.c")
        self.assertFalse(os.path.isabs(result))

    def test_in_cwd_output_arg_renders_relative(self):
        arg = f"{RUN_DIR}/gcc-pp.opts-O3.s"
        self.assertEqual(
            sc.resolve_argv_value(arg, INSTALL_ROOT, self.cwd), "gcc-pp.opts-O3.s"
        )

    def test_out_of_cwd_arg_is_absolute(self):
        arg = "./benchspec/CPU/502.gcc_r/data/refrate/input/somewhere.dat"
        result = sc.resolve_argv_value(arg, INSTALL_ROOT, self.cwd)
        self.assertTrue(os.path.isabs(result))
        self.assertEqual(
            result,
            str((INSTALL_ROOT / arg[2:]).resolve(strict=False)),
        )

    def test_bare_name_stem_passes_through_untouched(self):
        # e.g. 544.nab_r "1am0", 503.bwaves_r "bwaves_1", x264 "1280x720".
        for stem in ("1am0", "bwaves_1", "1280x720", "General"):
            self.assertEqual(sc.resolve_argv_value(stem, INSTALL_ROOT, self.cwd), stem)

    def test_non_path_flags_pass_through(self):
        for flag in ("-O3", "--frames", "1000", "-finline-limit=36000"):
            self.assertEqual(sc.resolve_argv_value(flag, INSTALL_ROOT, self.cwd), flag)

    def test_include_flag_in_cwd_stays_relative(self):
        arg = f"-I{RUN_DIR}/include"
        self.assertEqual(
            sc.resolve_argv_value(arg, INSTALL_ROOT, self.cwd), "-Iinclude"
        )

    def test_key_value_path_in_cwd_stays_relative(self):
        arg = f"--data={RUN_DIR}/input.txt"
        self.assertEqual(
            sc.resolve_argv_value(arg, INSTALL_ROOT, self.cwd), "--data=input.txt"
        )


class ResolveCommandTests(unittest.TestCase):
    def test_exe_absolute_args_relative_redirects_absolute(self):
        entry = {
            "cwd": RUN_DIR,
            "argv": [
                f"{RUN_DIR}/cpugcc_r_peak",
                f"{RUN_DIR}/gcc-pp.c",
                "-O3",
                "-o",
                f"{RUN_DIR}/gcc-pp.s",
            ],
            "redirects": {
                "stdout": f"{RUN_DIR}/out",
                "stderr-append": f"{RUN_DIR}/err",
            },
        }
        resolved = sc.resolve_command(entry, INSTALL_ROOT)
        # exe is absolute
        self.assertTrue(os.path.isabs(resolved.argv[0]))
        self.assertEqual(resolved.argv[0], f"{RUN_DIR_ABS}/cpugcc_r_peak")
        # in-cwd args are relative (the fix)
        self.assertEqual(resolved.argv[1:], ["gcc-pp.c", "-O3", "-o", "gcc-pp.s"])
        # cwd and redirect targets are absolute
        self.assertEqual(resolved.cwd, RUN_DIR_ABS)
        self.assertTrue(os.path.isabs(resolved.redirects["stdout"]))
        self.assertEqual(resolved.redirects["stdout"], f"{RUN_DIR_ABS}/out")

    def test_missing_argv_raises_with_context(self):
        with self.assertRaises(sc.UserError) as ctx:
            sc.resolve_command({"cwd": RUN_DIR}, INSTALL_ROOT, "command 7")
        self.assertIn("command 7", str(ctx.exception))

    def test_bad_cwd_raises(self):
        with self.assertRaises(sc.UserError):
            sc.resolve_command({"argv": ["x"], "cwd": 5}, INSTALL_ROOT)


class RenderRedirectsTests(unittest.TestCase):
    def test_order_and_quoting(self):
        rendered = sc.render_redirects(
            {"stderr-append": "e r", "stdout": "out", "stdin": "in"}
        )
        # stdin first, stdout next, stderr-append last; space-containing path quoted.
        self.assertEqual(rendered, "< in > out 2>> 'e r'")

    def test_unknown_key_raises(self):
        with self.assertRaises(sc.UserError):
            sc.render_redirects({"stdwhat": "x"})

    def test_conflicting_stdout_raises(self):
        with self.assertRaises(sc.UserError):
            sc.render_redirects({"stdout": "a", "stdout-append": "b"})


class FilterCommandsTests(unittest.TestCase):
    ENTRIES = [
        {"suite": "intrate", "benchmark": "502.gcc_r"},
        {"suite": "intrate", "benchmark": "525.x264_r"},
        {"suite": "fprate", "benchmark": "503.bwaves_r"},
    ]

    def test_include_is_case_insensitive_substring(self):
        out = sc.filter_commands(self.ENTRIES, include=["GCC"])
        self.assertEqual([e["benchmark"] for e in out], ["502.gcc_r"])

    def test_suite_filter(self):
        out = sc.filter_commands(self.ENTRIES, suites=["fprate"])
        self.assertEqual([e["benchmark"] for e in out], ["503.bwaves_r"])

    def test_exclude_after_include(self):
        out = sc.filter_commands(self.ENTRIES, exclude=["x264"])
        self.assertEqual(len(out), 2)

    def test_no_match_raises(self):
        with self.assertRaises(sc.UserError):
            sc.filter_commands(self.ENTRIES, include=["nosuch"])


if __name__ == "__main__":
    unittest.main()
