"""What umami prints: plain sentences for people, complete JSON for agents."""

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from umami import cli, system, ui
from tests.test_compose import Fixture

BODY = """
    [[transform]]
    id = "gaps"
    type = "lua.set"
    path = "general.gaps_out"
    value = 4
"""


def report(*statuses, status="ok", **extra):
    transforms = [{"id": f"t{i}", "type": "lua.set", "required": False, "status": s, "detail": "",
                   "warnings": [], "what": "general.gaps_in = 1"} for i, s in enumerate(statuses)]
    return {"flavor": "test", "status": status, "blocking": [], "transforms": transforms, **extra}


class VerdictTest(unittest.TestCase):
    def test_skipped_and_to_check_are_counted_apart(self):
        self.assertEqual(ui.verdict(report("drift", "stale", "stale", status="degraded")),
                         "Works, with 2 changes skipped and 1 change to check.")
        self.assertIn("t0 could not apply", ui.verdict(report("stale", status="failed", blocking=["t0"])))

    def test_a_failed_patch_reads_as_a_sentence(self):
        t = report("stale")["transforms"][0]
        t["detail"] = "no longer applies: a.diff: patch failed: default/x.json:46; b.diff: patch failed: y.lua:3"
        self.assertEqual(ui.detail_lines(t), ["a.diff doesn't fit default/x.json near line 46",
                                              "b.diff doesn't fit y.lua near line 3"])

    def test_notes_on_applied_changes_wait_for_verbose(self):
        t = report("applied")["transforms"][0]
        t["warnings"] = ["informational"]
        self.assertEqual(ui.detail_lines(t), [])
        self.assertEqual(ui.detail_lines(t, verbose=True), ["note: informational"])

    def test_the_pacman_lines_name_each_problem_and_where_to_look(self):
        lines = ui.one_line(report("applied", "stale", status="degraded")).splitlines()
        self.assertTrue(lines[1].startswith("umami:   t1: skipped"))
        self.assertIn("umami status", lines[-1])

    def test_only_a_skipped_change_gets_the_steps_to_fix_it(self):
        self.assertIn("sudo umami flavor x", "\n".join(ui.next_steps(report("stale", status="degraded"), "x")))
        self.assertNotIn("sudo", "\n".join(ui.next_steps(report("drift", status="degraded"), "x")))

    def test_the_error_label_is_red_only_on_a_terminal(self):
        for tty, coloured in ((True, True), (False, False)):
            err = io.StringIO()
            err.isatty = lambda tty=tty: tty
            with contextlib.redirect_stderr(err):
                ui.error("broken")
            self.assertEqual("\033[" in err.getvalue(), coloured)
            self.assertIn("Error:", err.getvalue())


class OutputTest(Fixture):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_plan_json_is_the_whole_report_and_nothing_else(self):
        self.recipe(BODY)
        code, out = self.run_cli("plan", str(self.recipe_dir / "recipe.toml"), "--source", str(self.source), "--json")
        data = json.loads(out)
        self.assertEqual(data["transforms"][0]["what"], "general.gaps_out = 4")
        self.assertIn("default/hypr/flavor.lua", data["files"]["added"])

    def test_plan_lists_files_only_when_asked(self):
        self.recipe(BODY)
        recipe = str(self.recipe_dir / "recipe.toml")
        code, out = self.run_cli("plan", recipe, "--source", str(self.source))
        self.assertNotIn("default/hypr/flavor.lua", out)
        code, out = self.run_cli("plan", recipe, "--source", str(self.source), "--files")
        self.assertIn("default/hypr/flavor.lua", out)

    def test_status_json_says_where_omarchy_runs_from(self):
        state = Path(self.tmp.name) / "state"
        state.mkdir()
        (state / "report.json").write_text(json.dumps(report("applied", composed_at="2026-11-18T16:48:37+00:00")))
        conf = state / "omarchy.conf"
        conf.write_text(f'export OMARCHY_PATH="{system.TREE}"\n')
        with mock.patch.object(system, "REPORT", state / "report.json"), mock.patch.object(system, "OMARCHY_CONF", conf), \
                mock.patch.dict(os.environ, {"OMARCHY_PATH": str(system.STOCK)}):
            code, out = self.run_cli("status", "--json")
            data = json.loads(out)
            self.assertEqual((data["next_boot"], data["this_session"]), (str(system.TREE), str(system.STOCK)))


if __name__ == "__main__":
    unittest.main()
