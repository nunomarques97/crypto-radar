from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from scripts import run_tests


class TestTestRunnerEnvironment(unittest.TestCase):
    def test_child_uses_disposable_paths_and_cannot_see_sensitive_values(self):
        with tempfile.TemporaryDirectory() as original_dir, tempfile.TemporaryDirectory() as state_dir:
            original = Path(original_dir)
            sentinel = original / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            base_environment = {
                "PATH": "C:\\Windows\\System32",
                "RADAR_STATE_DIR": str(original),
                "RADAR_SQLITE_PATH": str(original / "production.sqlite"),
                "RADAR_EVENTS_LOG_PATH": str(original / "events.jsonl"),
                "RADAR_OUTPUT_V08_PATH": str(original / "output.json"),
                "RADAR_ASSET_PAIRS_CACHE_PATH": str(original / "cache.json"),
                "ANTHROPIC_API_KEY": "seeded-cloud-secret",
                "ANTHROPIC_AUTH_TOKEN": "seeded-cloud-token",
                "KRAKEN_API_KEY": "seeded-kraken-key",
                "KRAKEN_SECRET": "seeded-kraken-secret",
                "KRAKEN_API_SECRET": "seeded-kraken-private-read-secret",
                "KRAKEN_PAIR": "kept-public-setting",
                "CRYPTO_RADAR_NTFY_TOPIC": "seeded-destination",
            }
            environment = run_tests.build_test_environment(state_dir, base_environment)
            child = """
import json
import os
from pathlib import Path
from radar_v08 import config

state = Path(os.environ[\"RADAR_STATE_DIR\"]).resolve()
paths = [
    config.STATE_DIR, config.SQLITE_PATH, config.ASSET_PAIRS_CACHE_PATH,
    config.RUN_LOG_PATH, config.TEXT_LOG_PATH, config.OUTPUT_V08_PATH,
    config.OUTPUT_V07_PATH, config.SHADOW_COMPARISON_PATH, config.EVENTS_LOG_PATH,
]
assert all(Path(path).resolve().is_relative_to(state) for path in paths)
blocked = [\"ANTHROPIC_API_KEY\", \"ANTHROPIC_AUTH_TOKEN\", \"KRAKEN_API_KEY\", \"KRAKEN_SECRET\", \"KRAKEN_API_SECRET\", \"CRYPTO_RADAR_NTFY_TOPIC\"]
assert not any(name in os.environ for name in blocked)
assert os.environ[\"KRAKEN_PAIR\"] == \"kept-public-setting\"
print(json.dumps({\"state\": str(state), \"paths_ok\": True, \"credentials_absent\": True}))
"""
            result = subprocess.run(
                [sys.executable, "-c", child],
                cwd=run_tests.REPOSITORY_ROOT,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["state"], str(Path(state_dir).resolve()))
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertEqual(list(original.iterdir()), [sentinel])

    def test_unittest_subprocess_exit_code_is_propagated(self):
        result = mock.Mock(returncode=23)
        with mock.patch.object(run_tests.subprocess, "run", return_value=result) as run:
            exit_code = run_tests.run_unittest_suite("unused-state", {"PATH": "node-path"})

        self.assertEqual(exit_code, 23)
        self.assertEqual(run.call_args.kwargs["cwd"], run_tests.REPOSITORY_ROOT)
        self.assertEqual(run.call_args.args[0], [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])

    def test_missing_node_is_a_setup_failure(self):
        with mock.patch.object(run_tests.shutil, "which", return_value=None), \
             mock.patch.object(run_tests, "run_unittest_suite") as run:
            exit_code = run_tests.main()

        self.assertEqual(exit_code, 2)
        run.assert_not_called()


SEEDED_SECRETS = {
    "ANTHROPIC_API_KEY": "seeded-cloud-secret",
    "ANTHROPIC_AUTH_TOKEN": "seeded-cloud-token",
    "KRAKEN_API_KEY": "seeded-kraken-key",
    "KRAKEN_SECRET": "seeded-kraken-secret",
    "KRAKEN_API_SECRET": "seeded-kraken-private-read-secret",
    "CRYPTO_RADAR_NTFY_TOPIC": "seeded-destination",
}

PASSING_WITH_SKIP = """
import unittest

class AlphaTests(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(True)

    @unittest.skip("not on this platform")
    def test_skipped(self):
        raise AssertionError("must not run")
"""

ONE_FAILURE = """
import unittest

class BetaTests(unittest.TestCase):
    def test_fails(self):
        self.assertEqual(1, 2)

    def test_passes(self):
        self.assertTrue(True)
"""

STAMP_AFTER_SLEEP = """
import pathlib
import time
import unittest

class {cls}(unittest.TestCase):
    def test_stamp(self):
        time.sleep({delay})
        pathlib.Path({stamp}).write_text(repr(time.time()))
"""

ENVIRONMENT_PROBE = """
import json
import os
import pathlib
import unittest

class ProbeTests(unittest.TestCase):
    def test_probe(self):
        pathlib.Path({report}).write_text(json.dumps({
            "state": os.environ.get("RADAR_STATE_DIR"),
            "radar": sorted(name for name in os.environ if name.startswith("RADAR_")),
            "present": sorted(name for name in {sensitive} if name in os.environ),
            "utf8": os.environ.get("PYTHONUTF8"),
            "no_bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
        }))
"""

ID_SHIFTING_MODULE = """
import os
import unittest

FIRST_IMPORT = not os.path.exists({marker})
if FIRST_IMPORT:
    open({marker}, "w").close()

class ShiftyTests(unittest.TestCase):
    def test_always(self):
        pass

def _check(self):
    pass

setattr(ShiftyTests, "test_only_in_discovery" if FIRST_IMPORT else "test_only_in_worker", _check)
"""


class TestParallelRunner(unittest.TestCase):
    """Synthetic test packages in temporary directories; the real suite is never run here."""

    def setUp(self):
        project = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        run_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(project.cleanup)
        self.addCleanup(run_dir.cleanup)
        self.project = Path(project.name)
        self.run_dir = Path(run_dir.name)
        (self.project / "tests").mkdir()

    def write_module(self, name, source, **values):
        text = textwrap.dedent(source)
        for key, value in values.items():
            text = text.replace("{" + key + "}", value if isinstance(value, str) else repr(str(value)))
        (self.project / "tests" / f"{name}.py").write_text(text, encoding="utf-8")

    def run_parallel(self, jobs=2, split_modules=(), base_environment=None):
        out = io.StringIO()
        exit_code = run_tests.run_parallel(
            self.run_dir,
            jobs=jobs,
            out=out,
            cwd=self.project,
            split_modules=split_modules,
            schedule_first=(),
            base_environment=dict(os.environ) if base_environment is None else base_environment,
        )
        return exit_code, out.getvalue()

    def test_a_failing_test_fails_the_run_while_other_units_still_report(self):
        self.write_module("test_alpha", PASSING_WITH_SKIP)
        self.write_module("test_beta", ONE_FAILURE)

        exit_code, output = self.run_parallel()

        self.assertEqual(exit_code, 1, output)
        self.assertIn("test_alpha.AlphaTests.test_pass) ... ok", output)
        self.assertIn("test_beta.BetaTests.test_passes) ... ok", output)
        self.assertIn("Failures:\n  test_beta.BetaTests.test_fails\n", output)
        self.assertIn("Ran 4 tests: failures 1, errors 0, skipped 1", output)
        self.assertTrue(output.rstrip().endswith("RESULT: FAILED"), output)

    def test_skips_are_counted_and_reported_with_their_reason(self):
        self.write_module("test_alpha", PASSING_WITH_SKIP)

        exit_code, output = self.run_parallel()

        self.assertEqual(exit_code, 0, output)
        self.assertIn("Ran 2 tests: failures 0, errors 0, skipped 1,", output)
        self.assertIn("Skipped:\n  test_alpha.AlphaTests.test_skipped: not on this platform\n", output)
        self.assertTrue(output.rstrip().endswith("RESULT: OK"), output)

    def test_a_worker_crash_fails_the_run(self):
        self.write_module("test_alpha", PASSING_WITH_SKIP)
        self.write_module(
            "test_crash",
            "import os\nimport unittest\n\nclass CrashTests(unittest.TestCase):\n    def test_crash(self):\n        os._exit(3)\n",
        )

        exit_code, output = self.run_parallel()

        self.assertEqual(exit_code, 1, output)
        self.assertIn("test_crash: worker exited with code 3 without a parsable result", output)
        self.assertIn("Missing test ids (discovered but not run):\n  test_crash.CrashTests.test_crash\n", output)
        self.assertIn("test_alpha.AlphaTests.test_pass) ... ok", output)
        self.assertTrue(output.rstrip().endswith("RESULT: FAILED"), output)

    def test_an_import_error_fails_discovery_and_listing(self):
        self.write_module("test_alpha", PASSING_WITH_SKIP)
        self.write_module("test_broken", "import module_that_does_not_exist_for_runner_tests\n")

        exit_code, output = self.run_parallel()
        listing = io.StringIO()
        list_code = run_tests.list_tests(self.run_dir / "list", out=listing, cwd=self.project)

        self.assertEqual(exit_code, 1, output)
        self.assertIn("Test discovery failed:", output)
        self.assertIn("test_broken", output)
        self.assertNotIn("----- unit", output)
        self.assertEqual(list_code, 1, listing.getvalue())
        self.assertIn("Test discovery failed:", listing.getvalue())

    def test_a_discovered_versus_ran_mismatch_fails_even_when_every_test_passes(self):
        self.write_module("test_shifty", ID_SHIFTING_MODULE, marker=self.project / "discovered.marker")

        exit_code, output = self.run_parallel()

        self.assertEqual(exit_code, 1, output)
        self.assertIn("Ran 2 tests: failures 0, errors 0", output)
        self.assertIn("Missing test ids (discovered but not run):\n  test_shifty.ShiftyTests.test_only_in_discovery\n", output)
        self.assertIn(
            "Unexpected or duplicate test ids (run but not discovered once):\n  test_shifty.ShiftyTests.test_only_in_worker\n",
            output,
        )
        self.assertIn("test_shifty: ran ids differ from discovery (1 missing, 1 extra or duplicate)", output)
        self.assertTrue(output.rstrip().endswith("RESULT: FAILED"), output)

    def test_id_comparison_detects_missing_extra_and_duplicate_ids(self):
        self.assertEqual(run_tests.compare_test_ids(["a", "b"], ["b", "a"]), ([], []))
        self.assertEqual(run_tests.compare_test_ids(["a", "b"], ["a"]), (["b"], []))
        self.assertEqual(run_tests.compare_test_ids(["a"], ["a", "c"]), ([], ["c"]))
        self.assertEqual(run_tests.compare_test_ids(["a", "b"], ["a", "b", "b"]), ([], ["b"]))
        self.assertEqual(run_tests.compare_test_ids(["a", "a"], ["a"]), (["a"], []))

    def test_output_follows_discovery_order_when_an_earlier_unit_finishes_last(self):
        stamps = self.project / "stamps"
        stamps.mkdir()
        self.write_module("test_a_slow", STAMP_AFTER_SLEEP, cls="SlowTests", delay="1.5", stamp=stamps / "slow")
        self.write_module("test_b_fast", STAMP_AFTER_SLEEP, cls="FastTests", delay="0", stamp=stamps / "fast")

        exit_code, output = self.run_parallel(jobs=2)

        self.assertEqual(exit_code, 0, output)
        self.assertLess(float((stamps / "fast").read_text()), float((stamps / "slow").read_text()))
        self.assertLess(output.index("unit 1/2: test_a_slow"), output.index("unit 2/2: test_b_fast"))
        self.assertLess(
            output.index("test_a_slow.SlowTests.test_stamp) ... ok"),
            output.index("test_b_fast.FastTests.test_stamp) ... ok"),
        )

    def test_workers_get_distinct_state_dirs_and_no_sensitive_variables(self):
        reports = self.project / "reports"
        reports.mkdir()
        original = self.project / "original-state"
        original.mkdir()
        sentinel = original / "sentinel.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        names = ("test_probe_one", "test_probe_two")
        for name in names:
            self.write_module(name, ENVIRONMENT_PROBE, report=reports / f"{name}.json", sensitive=repr(sorted(SEEDED_SECRETS)))
        base_environment = dict(os.environ)
        base_environment.update(SEEDED_SECRETS)
        base_environment["RADAR_STATE_DIR"] = str(original)
        base_environment["RADAR_SQLITE_PATH"] = str(original / "production.sqlite")

        exit_code, output = self.run_parallel(base_environment=base_environment)

        self.assertEqual(exit_code, 0, output)
        seen = [json.loads((reports / f"{name}.json").read_text()) for name in names]
        states = [Path(report["state"]).resolve() for report in seen]
        self.assertNotEqual(states[0], states[1])
        for report, state in zip(seen, states):
            self.assertTrue(state.is_relative_to(self.run_dir.resolve()), state)
            self.assertEqual(report["radar"], ["RADAR_STATE_DIR"])
            self.assertEqual(report["present"], [])
            self.assertEqual(report["utf8"], "1")
            self.assertEqual(report["no_bytecode"], "1")
        self.assertEqual(sorted(original.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_a_split_module_runs_each_class_as_its_own_unit(self):
        other = "\nclass OtherTests(unittest.TestCase):\n    def test_other(self):\n        self.assertTrue(True)\n"
        self.write_module("test_alpha", PASSING_WITH_SKIP + other)
        self.write_module("test_gamma", "import unittest\n\nclass GammaTests(unittest.TestCase):\n    def test_gamma(self):\n        pass\n")

        exit_code, output = self.run_parallel(split_modules={"test_alpha"})

        self.assertEqual(exit_code, 0, output)
        self.assertIn("unit 1/3: test_alpha.AlphaTests (2 tests)", output)
        self.assertIn("unit 2/3: test_alpha.OtherTests (1 tests)", output)
        self.assertIn("unit 3/3: test_gamma (1 tests)", output)
        self.assertIn("Ran 4 tests: failures 0, errors 0, skipped 1", output)

    def test_list_prints_discovered_ids_and_a_count(self):
        self.write_module("test_alpha", PASSING_WITH_SKIP)
        self.write_module("test_beta", ONE_FAILURE)
        listing = io.StringIO()

        exit_code = run_tests.list_tests(self.run_dir, out=listing, cwd=self.project)

        self.assertEqual(exit_code, 0, listing.getvalue())
        self.assertEqual(
            listing.getvalue().splitlines(),
            [
                "test_alpha.AlphaTests.test_pass",
                "test_alpha.AlphaTests.test_skipped",
                "test_beta.BetaTests.test_fails",
                "test_beta.BetaTests.test_passes",
                "4 tests discovered in 2 modules",
            ],
        )


class TestRunnerModes(unittest.TestCase):
    def test_default_mode_is_parallel_and_serial_mode_keeps_the_discover_run(self):
        with mock.patch.object(run_tests.shutil, "which", return_value="node"), \
             mock.patch.object(run_tests, "run_unittest_suite", return_value=5) as serial, \
             mock.patch.object(run_tests, "run_parallel", return_value=7) as parallel:
            self.assertEqual(run_tests.main(), 7)
            self.assertEqual(parallel.call_count, 1)
            serial.assert_not_called()
            self.assertEqual(run_tests.main(["--serial"]), 5)
            self.assertEqual(serial.call_count, 1)
            self.assertEqual(parallel.call_count, 1)

    def test_missing_node_starts_no_suite_in_either_mode(self):
        for argv in ([], ["--serial"], ["--jobs", "3"]):
            with self.subTest(argv=argv), mock.patch.object(run_tests.shutil, "which", return_value=None), \
                 mock.patch.object(run_tests, "run_unittest_suite") as serial, \
                 mock.patch.object(run_tests, "run_parallel") as parallel:
                self.assertEqual(run_tests.main(argv), 2)
                serial.assert_not_called()
                parallel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
