import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from umami import lua
from umami.compose import Composer
from umami.recipe import RecipeError, load


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

    def recipe(self, body: str, code=True):
        head = '[flavor]\nname = "test"\n' + ("code = true\n" if code else "") + "\n"
        write(self.recipe_dir / "recipe.toml", head + textwrap.dedent(body))
        return load(self.recipe_dir / "recipe.toml")

    def compose(self, body: str, code=True):
        report = Composer(self.recipe(body, code), self.source, self.dest).run()
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

    def test_flavor_lua_loads_before_theme_overrides(self):
        entry = self.source / "default/hypr/omarchy.lua"
        entry.write_text(entry.read_text().replace(
            'require("default.hypr.software-rendering")',
            'require_optional.module("omarchy.current.theme.hyprland")\nrequire("default.hypr.software-rendering")'))
        self.compose("""
            [[transform]]
            id = "gaps"
            type = "lua.set"
            path = "general.gaps_out"
            value = 4
        """)
        lines = (self.dest / "default/hypr/omarchy.lua").read_text().splitlines()
        self.assertEqual(lines.index('require_optional.module("default.hypr.flavor")') + 1,
                         lines.index('require_optional.module("omarchy.current.theme.hyprland")'))

    def test_lua_code_inline_and_from_a_file(self):
        write(self.recipe_dir / "extra.lua", "hl.unbind(\"SUPER + Q\")\n")
        report, t = self.compose("""
            [[transform]]
            id = "inline"
            type = "lua"
            code = 'hl.unbind("SUPER + X")'

            [[transform]]
            id = "file"
            type = "lua"
            source = "extra.lua"
        """)
        self.assertEqual(report["status"], "ok")
        module = (self.dest / "default/hypr/flavor.lua").read_text()
        self.assertLess(module.index('hl.unbind("SUPER + X")'), module.index('hl.unbind("SUPER + Q")'))

    def test_lua_needs_exactly_one_of_code_and_source(self):
        for body in ('id = "x"\ntype = "lua"', 'id = "x"\ntype = "lua"\ncode = "a()"\nsource = "a.lua"'):
            with self.assertRaises(RecipeError):
                self.recipe("[[transform]]\n" + body + "\n")

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


class TierTest(Fixture):
    """A flavor is declarative unless it says code = true (tiers.py)."""

    def test_a_declarative_flavor_composes_without_code(self):
        report, t = self.compose("""
            [[transform]]
            id = "gaps"
            type = "lua.set"
            path = "general.gaps_out"
            value = 4

            [[transform]]
            id = "focus"
            type = "bind.replace"
            keys = "super+return"
            description = "Focus left"
            action = { focus = "l" }
        """, code=False)
        self.assertEqual(report["status"], "ok")
        module = (self.dest / "default/hypr/flavor.lua").read_text()
        self.assertIn('o.rebind("super+return", "Focus left", hl.dsp.focus({ direction = "l" }))', module)

    def test_code_needs_to_be_declared(self):
        for body in ('id = "x"\ntype = "lua"\ncode = "os.execute(1)"',
                     'id = "x"\ntype = "bind.add"\nkeys = "super+z"\ndescription = "x"\nrun = "curl x | sh"',
                     'id = "x"\ntype = "bind.add"\nkeys = "super+z"\ndescription = "x"\nlua = "os.exit()"',
                     'id = "x"\ntype = "lua.set"\npath = "input.kb_layout"\nvalue = "us"',
                     'id = "x"\ntype = "file.remove"\npath = "bin/omarchy-git-url-check"'):
            with self.assertRaisesRegex(RecipeError, "run code"):
                self.recipe("[[transform]]\n" + body + "\n", code=False)

    PLUGIN = '[[plugin]]\nid = "acme.switch"\nrepo = "https://example.com/switch"\ncommit = "' + "a" * 40 + '"\n\n'

    def test_a_plugins_own_hyprland_file_loads_declaratively(self):
        report, t = self.compose(self.PLUGIN + """
            [[transform]]
            id = "switch-keys"
            type = "plugin.hypr"
            plugin = "acme.switch"
            file = "hypr/switch.lua"
        """, code=False)
        self.assertEqual(t["switch-keys"]["status"], "applied")
        module = (self.dest / "default/hypr/flavor.lua").read_text()
        self.assertIn('(os.getenv("HOME") or "") .. "/.config/omarchy/plugins/acme.switch/hypr/switch.lua"', module)
        self.assertIn("pcall(dofile, file)", module)

    def test_plugin_hypr_stays_inside_one_of_the_recipes_plugins(self):
        for plugin, file in (("acme.switch", "../../.bashrc"), ("acme.switch", "/etc/x.lua"),
                             ("acme.switch", "hypr/x.sh"), ("acme.switch", ".hidden/x.lua"),
                             ("acme.other", "hypr/x.lua")):
            with self.assertRaises(RecipeError):
                self.recipe(self.PLUGIN + f'[[transform]]\nid = "x"\ntype = "plugin.hypr"\nplugin = "{plugin}"\nfile = "{file}"\n',
                            code=False)

    def test_actions_are_checked(self):
        for action in ('{ focus = "x" }', '{ omarchy = "omarchy-x; rm -rf ~" }', '{ omarchy = "bash -c x" }',
                       '{ run = "x" }', '{ focus = "l", swap = "r" }'):
            with self.assertRaises(RecipeError):
                self.recipe(f'[[transform]]\nid = "x"\ntype = "bind.add"\nkeys = "super+z"\ndescription = "x"\naction = {action}\n',
                            code=False)

    def test_a_declarative_omarchy_action_must_be_an_omarchy_command(self):
        report, t = self.compose("""
            [[transform]]
            id = "k"
            type = "bind.add"
            keys = "super+z"
            description = "x"
            action = { omarchy = "omarchy-not-a-command" }
        """, code=False)
        self.assertEqual(t["k"]["status"], "stale")

    def test_a_declarative_flavor_cannot_remove_a_service(self):
        write(self.source / "shell/plugins/lock/manifest.json", '{"id": "omarchy.lock", "kinds": ["service"]}')
        report, t = self.compose("""
            [[transform]]
            id = "nolock"
            type = "shell.plugins.remove"
            ids = ["omarchy.lock"]
        """, code=False)
        self.assertNotEqual(report["status"], "ok")
        self.assertTrue((self.dest / "shell/plugins/lock/manifest.json").exists())

    def test_an_optional_unbind_of_a_free_key_is_fine(self):
        report, t = self.compose("""
            [[transform]]
            id = "free"
            type = "bind.remove"
            keys = "super+ctrl+x"
            optional = true
        """, code=False)
        self.assertEqual(t["free"]["status"], "applied")


class LockedTest(Fixture):
    """Defaults the user can change; locks the flavor keeps."""

    LOCKED = """
        [[transform]]
        id = "corners"
        type = "lua.set"
        path = "decoration.rounding"
        value = 0
        locked = true

        [[transform]]
        id = "gaps"
        type = "lua.set"
        path = "general.gaps_out"
        value = 4
    """

    def test_a_locked_transform_loads_after_the_users_config(self):
        write(self.source / "default/hypr/toggles.lua", "require_all.files(toggles_dir)\n")
        report, t = self.compose(self.LOCKED, code=False)
        self.assertEqual(report["status"], "ok")
        locked = (self.dest / "default/hypr/flavor-locked.lua").read_text()
        self.assertIn("rounding = 0", locked)
        self.assertNotIn("gaps_out", locked)
        self.assertNotIn("rounding", (self.dest / "default/hypr/flavor.lua").read_text())
        toggles = (self.dest / "default/hypr/toggles.lua").read_text()
        self.assertTrue(toggles.rstrip().endswith('require("default.hypr.flavor-locked")'))
        self.assertTrue(t["corners"]["locked"])
        self.assertFalse(t["gaps"]["locked"])

    def test_without_toggles_lua_a_lock_is_reported_as_not_holding(self):
        report, t = self.compose(self.LOCKED, code=False)
        self.assertEqual(t["corners"]["status"], "drift")
        self.assertTrue(any("do not hold" in w for w in t["corners"]["warnings"]))
        self.assertIn('require("default.hypr.flavor-locked")', (self.dest / "default/hypr/flavor.lua").read_text())

    def test_removing_a_plugin_from_the_tree_is_a_lock(self):
        report, t = self.compose("""
            [[transform]]
            id = "no-weather"
            type = "shell.plugins.remove"
            ids = ["omarchy.weather"]
        """, code=False)
        self.assertTrue(t["no-weather"]["locked"])

    def test_only_some_transforms_can_be_locked(self):
        with self.assertRaises(RecipeError):
            self.recipe('[[transform]]\nid = "x"\ntype = "file.remove"\npath = "version"\nlocked = true\n')
        with self.assertRaises(RecipeError):
            self.recipe('[[transform]]\nid = "x"\ntype = "bind.remove"\nkeys = "super+q"\nlocked = "yes"\n')

    def test_the_locked_table_is_checked_like_defaults(self):
        self.assertEqual(self.recipe('[locked]\ntheme = "Nord"\n', code=False).locked, {"theme": "Nord"})
        for body in ('[locked]\nfont = "x"\n', '[locked.bar]\nmove = [{ id = "x", section = "middle" }]\n'):
            with self.assertRaises(RecipeError):
                self.recipe(body, code=False)

    def test_a_locked_table_alone_starts_the_user_layer(self):
        self.compose('[locked]\ntheme = "Nord"\n', code=False)
        self.assertIn("user-sync", (self.dest / "default/hypr/flavor.lua").read_text())


class BuiltinPatchTest(Fixture):
    def test_the_flavor_tree_counts_as_a_package_install(self):
        write(self.source / "bin/omarchy-version",
              '#!/bin/bash\nif [[ $omarchy_path != "/usr/share/omarchy" ]]; then\n  echo dev\nfi\n', mode=0o755)
        report, t = self.compose("", code=False)
        builtin = {p["file"]: p["status"] for p in report["builtin"]}
        self.assertEqual(builtin["bin/omarchy-version"], "applied")
        self.assertIn('$omarchy_path != "/var/lib/umami/tree"', (self.dest / "bin/omarchy-version").read_text())
        # Scripts this Omarchy doesn't have, or has changed, are reported, not fatal.
        self.assertEqual(builtin["bin/omarchy-channel-set"], "stale")
        self.assertEqual(report["status"], "ok")

    def test_enabling_a_plugin_the_flavor_removed_says_why(self):
        write(self.source / "bin/omarchy-plugin-enable",
              '#!/bin/bash\nif [[ $result == "unknown" ]]; then\n'
              '  fail "plugin \'$id\' is not known; run: omarchy-shell shell rescanPlugins"\nfi\n', mode=0o755)
        report, t = self.compose("""
            [[transform]]
            id = "no-weather"
            type = "shell.plugins.remove"
            ids = ["omarchy.weather"]
        """, code=False)
        self.assertIn("this machine's flavor removes it", (self.dest / "bin/omarchy-plugin-enable").read_text())
        self.assertEqual((self.dest / ".flavor/removed").read_text(), "omarchy.weather\n")

    def test_switching_channel_keeps_the_flavor_linked(self):
        write(self.source / "bin/omarchy-channel-set",
              '#!/bin/bash\nif [[ -z $dev_checkout ]]; then\n  omarchy-dev-unlink --no-reboot\n'
              '  export OMARCHY_PATH=/usr/share/omarchy\nfi\n', mode=0o755)
        report, t = self.compose("", code=False)
        statuses = [p["status"] for p in report["builtin"] if p["file"] == "bin/omarchy-channel-set"]
        # The leaving_dev line is missing from this script; the other two apply.
        self.assertEqual(statuses, ["stale", "applied", "applied"])
        text = (self.dest / "bin/omarchy-channel-set").read_text()
        self.assertIn('[[ $OMARCHY_PATH == "/var/lib/umami/tree" ]] || omarchy-dev-unlink --no-reboot', text)
        self.assertIn('[[ $OMARCHY_PATH == "/var/lib/umami/tree" ]] || export OMARCHY_PATH=/usr/share/omarchy', text)


class FindFlavorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.own, self.shipped = root / "own", root / "shipped"
        for d, name in [(self.own, "jobo-zen"), (self.own, "zen"), (self.shipped, "zen"), (self.shipped, "company")]:
            write(d / name / "recipe.toml", f'[flavor]\nname = "{name}"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_your_own_flavor_is_found_by_name(self):
        from umami.system import find_flavor
        self.assertEqual(find_flavor("jobo-zen", [self.own, self.shipped]), self.own / "jobo-zen")
        self.assertEqual(find_flavor("company", [self.own, self.shipped]), self.shipped / "company")

    def test_your_own_flavor_wins_over_a_shipped_one_of_the_same_name(self):
        from umami.system import find_flavor
        self.assertEqual(find_flavor("zen", [self.own, self.shipped]), self.own / "zen")

    def test_an_unknown_name_lists_every_flavor(self):
        from umami.system import find_flavor
        with self.assertRaises(SystemExit) as e:
            find_flavor("nope", [self.own, self.shipped, Path(self.tmp.name) / "missing"])
        self.assertIn("company, jobo-zen, zen", str(e.exception))
