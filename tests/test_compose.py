import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from omarchy_flavor import lua
from omarchy_flavor.compose import Composer
from omarchy_flavor.recipe import RecipeError, load


def write(path: Path, text: str, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"))
    if mode:
        path.chmod(mode)


class Fixture(unittest.TestCase):
    """A miniature Omarchy tree shaped like the real package."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root / "omarchy"
        self.outside = root / "usr-bin"
        self.recipe_dir = root / "recipe"
        self.dest = root / "composed"

        write(self.source / "version", "4.5.0")
        write(self.source / "default/hypr/omarchy.lua", """
            local require_optional = require("default.hypr.require_optional")
            require("default.hypr.looknfeel")
            require("default.hypr.software-rendering")
        """)
        write(self.source / "default/hypr/looknfeel.lua", "hl.config({ general = { gaps_out = 10 } })\n")
        write(self.source / "default/hypr/bindings/applications.lua", 'o.bind("SUPER + RETURN", "Terminal", o.launch_terminal())\n')
        # The package's bin/ is symlinks into /usr/bin.
        write(self.outside / "omarchy-hello", '#!/bin/bash\n# omarchy:summary=Say hello\necho "hello $*"\n', 0o755)
        write(self.outside / "omarchy-root-thing", "#!/bin/bash\nomarchy_security_require_source_root \"$0\"\n", 0o755)
        (self.source / "bin").mkdir()
        for name in ("omarchy-hello", "omarchy-root-thing"):
            os.symlink(self.outside / name, self.source / "bin" / name)
        write(self.source / "bin/omarchy-script", "pids=()\nwait\n", 0o755)
        write(self.source / "shell/shell.qml", "ShellIpc {\n  target: \"shell\"\n  function toggle(): void {}\n}\n")
        write(self.source / "shell/plugins/panels/weather/manifest.json", json.dumps({"id": "omarchy.weather"}))
        write(self.source / "shell/plugins/panels/weather/Panel.qml", textwrap.dedent("""
            Item {
              ShellIpc {
                target: "weather"
                function open(): void {}
                function refresh(): void {}
              }
            }
        """))

    def tearDown(self):
        self.tmp.cleanup()

    def recipe(self, body: str):
        write(self.recipe_dir / "recipe.toml", '[flavor]\nname = "test"\n\n' + textwrap.dedent(body))
        return load(self.recipe_dir / "recipe.toml")

    def compose(self, body: str):
        report = Composer(self.recipe(body), self.source, self.dest).run()
        return report, {t["id"]: t for t in report["transforms"]}


class ComposeTest(Fixture):
    def test_stale_transform_is_skipped_and_the_rest_applies(self):
        report, t = self.compose("""
            [[transform]]
            id = "gone"
            type = "bind.replace"
            keys = "SUPER + Z"
            description = "Nothing"
            run = "true"

            [[transform]]
            id = "gaps"
            type = "lua.set"
            path = "general.gaps_out"
            value = 4
        """)
        self.assertEqual(t["gone"]["status"], "stale")
        self.assertEqual(t["gaps"]["status"], "applied")
        self.assertEqual(report["status"], "degraded")
        self.assertIn("gaps_out = 4", (self.dest / "default/hypr/flavor.lua").read_text())
        self.assertNotIn("SUPER + Z", (self.dest / "default/hypr/flavor.lua").read_text())

    def test_required_stale_transform_fails_the_composition(self):
        report, _ = self.compose("""
            [[transform]]
            id = "must"
            type = "file.remove"
            path = "default/hypr/not-there.lua"
            required = true
        """)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["blocking"], ["must"])

    def test_flavor_lua_loads_before_software_rendering(self):
        self.compose("""
            [[transform]]
            id = "key"
            type = "bind.replace"
            keys = "super+return"
            description = "Terminal"
            run = { launch = "foot" }
        """)
        entry = (self.dest / "default/hypr/omarchy.lua").read_text()
        self.assertLess(entry.index("default.hypr.flavor"), entry.index("software-rendering"))
        module = (self.dest / "default/hypr/flavor.lua").read_text()
        self.assertIn('o.rebind("super+return", "Terminal", { launch = "foot" })', module)

    def test_bin_symlinks_become_real_files(self):
        self.compose("")
        hello = self.dest / "bin/omarchy-hello"
        self.assertFalse(hello.is_symlink())
        self.assertTrue(os.access(hello, os.X_OK))

    def test_wrapped_command_runs_upstream_then_after_hook(self):
        _, t = self.compose("""
            [[transform]]
            id = "wrap"
            type = "command.wrap"
            command = "omarchy-hello"
            after = 'echo "after $*"'
        """)
        self.assertEqual(t["wrap"]["status"], "applied")
        wrapper = self.dest / "bin/omarchy-hello"
        self.assertIn("# omarchy:summary=Say hello", wrapper.read_text())
        env = {k: v for k, v in os.environ.items() if k != "OMARCHY_PATH"}
        out = subprocess.run([str(wrapper), "world"], capture_output=True, text=True, env=env)
        self.assertEqual(out.stdout, "hello world\nafter world\n")

    def test_privileged_entrypoint_cannot_be_wrapped(self):
        _, t = self.compose("""
            [[transform]]
            id = "wrap"
            type = "command.wrap"
            command = "omarchy-root-thing"
            before = "true"
        """)
        self.assertEqual(t["wrap"]["status"], "stale")
        self.assertIn("privileged", t["wrap"]["detail"])

    def test_patch_needs_exactly_one_anchor(self):
        _, t = self.compose("""
            [[transform]]
            id = "ok"
            type = "patch"
            file = "bin/omarchy-script"
            anchor = "pids=()"
            replace_with = "running=0"

            [[transform]]
            id = "missing"
            type = "patch"
            file = "bin/omarchy-script"
            anchor = "pids=()"
            insert_after = "x"
        """)
        self.assertEqual(t["ok"]["status"], "applied")
        self.assertEqual(t["missing"]["status"], "stale")
        self.assertEqual((self.dest / "bin/omarchy-script").read_text(), "running=0\nwait\n")

    def test_file_replacement_reports_upstream_drift(self):
        write(self.recipe_dir / "files/looknfeel.lua", "-- mine\n")
        _, t = self.compose("""
            [[transform]]
            id = "replace"
            type = "file.add"
            path = "default/hypr/looknfeel.lua"
            source = "files/looknfeel.lua"
            upstream_sha256 = "0000"
        """)
        self.assertEqual(t["replace"]["status"], "drift")
        self.assertEqual((self.dest / "default/hypr/looknfeel.lua").read_text(), "-- mine\n")

    def test_plugin_replacement_reports_missing_ipc(self):
        write(self.recipe_dir / "weather/manifest.json", json.dumps({"id": "omarchy.weather"}))
        write(self.recipe_dir / "weather/Panel.qml", 'ShellIpc {\n  target: "weather"\n  function open(): void {}\n}\n')
        _, t = self.compose("""
            [[transform]]
            id = "w"
            type = "shell.plugins.replace"
            plugin = "omarchy.weather"
            source = "weather"
        """)
        self.assertEqual(t["w"]["status"], "drift")
        self.assertIn("weather.refresh", t["w"]["warnings"][0])
        self.assertFalse((self.dest / "shell/plugins/panels/weather/manifest.json").exists())
        self.assertTrue((self.dest / "shell/plugins/flavor/omarchy.weather/manifest.json").exists())

    def test_removed_plugin_keeps_its_code(self):
        _, t = self.compose("""
            [[transform]]
            id = "rm"
            type = "shell.plugins.remove"
            ids = ["omarchy.weather", "omarchy.gone"]
        """)
        self.assertEqual(t["rm"]["status"], "drift")
        self.assertFalse((self.dest / "shell/plugins/panels/weather/manifest.json").exists())
        self.assertTrue((self.dest / "shell/plugins/panels/weather/Panel.qml").exists())

    def test_paths_cannot_escape_the_tree(self):
        _, t = self.compose("""
            [[transform]]
            id = "escape"
            type = "file.remove"
            path = "../omarchy/version"
        """)
        self.assertEqual(t["escape"]["status"], "failed")
        self.assertTrue((self.source / "version").exists())


    def test_diff_applies_edits_new_files_and_modes(self):
        write(self.recipe_dir / "change.diff", """
            diff --git a/bin/omarchy-script b/bin/omarchy-script
            --- a/bin/omarchy-script
            +++ b/bin/omarchy-script
            @@ -1,2 +1,2 @@
            -pids=()
            +running=0
             wait
            diff --git a/bin/omarchy-new b/bin/omarchy-new
            new file mode 100755
            --- /dev/null
            +++ b/bin/omarchy-new
            @@ -0,0 +1,2 @@
            +#!/bin/bash
            +echo new
        """)
        _, t = self.compose("""
            [[transform]]
            id = "pr"
            type = "diff"
            source = "change.diff"
        """)
        self.assertEqual(t["pr"]["status"], "applied", t["pr"])
        self.assertEqual((self.dest / "bin/omarchy-script").read_text(), "running=0\nwait\n")
        self.assertTrue(os.access(self.dest / "bin/omarchy-new", os.X_OK))
        self.assertEqual(sorted(t["pr"]["changed"]), ["bin/omarchy-new", "bin/omarchy-script"])
        self.assertEqual((self.source / "bin/omarchy-script").read_text(), "pids=()\nwait\n")

    def test_diff_that_no_longer_applies_is_stale_and_changes_nothing(self):
        write(self.recipe_dir / "change.diff", """
            diff --git a/bin/omarchy-script b/bin/omarchy-script
            --- a/bin/omarchy-script
            +++ b/bin/omarchy-script
            @@ -1,2 +1,2 @@
            -pids=(old)
            +running=0
             wait
            diff --git a/default/hypr/looknfeel.lua b/default/hypr/looknfeel.lua
            --- a/default/hypr/looknfeel.lua
            +++ b/default/hypr/looknfeel.lua
            @@ -1 +1 @@
            -hl.config({ general = { gaps_out = 10 } })
            +hl.config({ general = { gaps_out = 2 } })
        """)
        _, t = self.compose("""
            [[transform]]
            id = "pr"
            type = "diff"
            source = "change.diff"
        """)
        self.assertEqual(t["pr"]["status"], "stale")
        self.assertIn("no longer applies", t["pr"]["detail"])
        # The hunk that still matches is not applied on its own.
        self.assertIn("gaps_out = 10", (self.dest / "default/hypr/looknfeel.lua").read_text())

    def test_diff_sources_use_the_first_that_applies(self):
        write(self.recipe_dir / "edge.diff", """
            diff --git a/bin/omarchy-script b/bin/omarchy-script
            --- a/bin/omarchy-script
            +++ b/bin/omarchy-script
            @@ -1,2 +1,2 @@
            -pids=(edge)
            +running=0
             wait
        """)
        write(self.recipe_dir / "stable.diff", """
            diff --git a/bin/omarchy-script b/bin/omarchy-script
            --- a/bin/omarchy-script
            +++ b/bin/omarchy-script
            @@ -1,2 +1,2 @@
            -pids=()
            +running=1
             wait
        """)
        _, t = self.compose("""
            [[transform]]
            id = "pr"
            type = "diff"
            sources = ["edge.diff", "stable.diff"]
        """)
        self.assertEqual(t["pr"]["status"], "applied", t["pr"])
        self.assertEqual(t["pr"]["warnings"], ["applied stable.diff"])
        self.assertEqual((self.dest / "bin/omarchy-script").read_text(), "running=1\nwait\n")

    def test_diff_needs_source_or_sources(self):
        with self.assertRaises(RecipeError):
            self.recipe("""
                [[transform]]
                id = "pr"
                type = "diff"
            """)

    def test_diff_cannot_write_outside_the_tree(self):
        write(self.recipe_dir / "escape.diff", """
            diff --git a/../escaped b/../escaped
            new file mode 100644
            --- /dev/null
            +++ b/../escaped
            @@ -0,0 +1 @@
            +out
        """)
        _, t = self.compose("""
            [[transform]]
            id = "escape"
            type = "diff"
            source = "escape.diff"
        """)
        self.assertNotEqual(t["escape"]["status"], "applied")
        self.assertFalse((self.dest.parent / "escaped").exists())

class RecipeTest(Fixture):
    def test_unknown_type_is_rejected(self):
        with self.assertRaises(RecipeError):
            self.recipe('[[transform]]\nid = "x"\ntype = "nope"\n')

    def test_patch_needs_one_action(self):
        with self.assertRaises(RecipeError):
            self.recipe('[[transform]]\nid = "x"\ntype = "patch"\nfile = "a"\nanchor = "b"\n')


class LuaTest(unittest.TestCase):
    def test_literal(self):
        self.assertEqual(lua.literal({"general": {"gaps_out": 4, "layout": "dwindle"}}),
                         '{ general = { gaps_out = 4, layout = "dwindle" } }')
        self.assertEqual(lua.literal(['a"b', True, 1.5]), '{ "a\\"b", true, 1.5 }')

    def test_normalize_keys(self):
        self.assertEqual(lua.normalize_keys("super+shift + return"), "SUPER + SHIFT + RETURN")


if __name__ == "__main__":
    unittest.main()
