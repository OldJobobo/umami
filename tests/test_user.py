import json
import os
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from umami import user
from umami.recipe import RecipeError, load

# Stand-ins for Omarchy's commands: each logs its arguments, and the validator
# refuses a plugin whose manifest says "invalid".
FAKES = {
    "omarchy-plugin-validate": 'echo "validate $*" >> "$FAKE_LOG"; ! grep -q invalid "$1/manifest.json"',
    "omarchy-plugin-enable": 'echo "enable $*" >> "$FAKE_LOG"',
    "omarchy-plugin-list": 'echo "[{\\"id\\": \\"acme.dock\\"}]"',
    "omarchy-shell": 'echo "shell $*" >> "$FAKE_LOG"',
    "notify-send": 'echo "notify $*" >> "$FAKE_LOG"',
    "omarchy-theme-set": 'echo "theme $*" >> "$FAKE_LOG"; echo "$*" > "$FAKE_LOG.theme"',
    "omarchy-theme-current": 'cat "$FAKE_LOG.theme" 2>/dev/null || echo Unknown',
    "omarchy-bar": 'echo "bar $*" >> "$FAKE_LOG"',
    "omarchy-plugin-disable": 'echo "disable $*" >> "$FAKE_LOG"; [[ $1 != *missing* ]] || { echo "plugin \'$1\' is not known" >&2; exit 1; }',
    "omarchy-git-url-check": 'echo "urlcheck $*" >> "$FAKE_LOG"; [[ $1 != *evil* ]]',
    "omarchy-launch-floating-terminal-with-presentation": 'echo "review $*" >> "$FAKE_LOG"',
}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class UserLayerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.log = root / "calls.log"
        self.log.touch()
        fakes = root / "bin"
        fakes.mkdir()
        for name, body in FAKES.items():
            (fakes / name).write_text("#!/bin/bash\n" + body + "\n")
            (fakes / name).chmod(0o755)
        self.saved_env = dict(os.environ)
        os.environ.update({"HOME": str(self.home), "PATH": f"{fakes}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
                           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
        for key in ("XDG_STATE_HOME", "XDG_CACHE_HOME"):
            os.environ.pop(key, None)

        # A plugin repository with two commits.
        self.repo = root / "acme-dock"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "t")
        self.write_manifest("acme.dock", "1")
        self.first = git(self.repo, "rev-parse", "HEAD")
        self.write_manifest("acme.dock", "2")
        self.second = git(self.repo, "rev-parse", "HEAD")
        self.recipe_dir = root / "recipe"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved_env)
        self.tmp.cleanup()

    def write_manifest(self, pid, version, extra=""):
        (self.repo / "manifest.json").write_text(json.dumps({"id": pid, "version": version, "note": extra}))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", f"v{version}")

    def recipe(self, commit, section='section = "right"'):
        self.recipe_dir.mkdir(exist_ok=True)
        path = self.recipe_dir / "recipe.toml"
        path.write_text(textwrap.dedent(f"""
            [flavor]
            name = "test"

            [[plugin]]
            id = "acme.dock"
            repo = "{self.repo}"
            commit = "{commit}"
            {section}
        """))
        return path

    def sync(self, commit, **kwargs):
        kwargs.setdefault("assume_yes", True)
        return user.sync(self.recipe(commit), shell_timeout=1, **kwargs)

    @property
    def installed(self):
        return self.home / ".config/omarchy/plugins/acme.dock"

    def test_installs_the_pinned_commit_and_enables_it_once(self):
        self.assertEqual(self.sync(self.first), 0)
        self.assertEqual(json.loads((self.installed / "manifest.json").read_text())["version"], "1")
        self.assertEqual(git(self.installed, "rev-parse", "HEAD"), self.first)
        calls = self.log.read_text()
        self.assertIn("validate", calls)
        self.assertIn("enable acme.dock --section right", calls)
        self.log.write_text("")
        self.assertEqual(self.sync(self.first), 0)
        self.assertNotIn("enable", self.log.read_text())

    def test_a_new_pin_updates_without_re_enabling(self):
        self.sync(self.first)
        self.log.write_text("")
        self.assertEqual(self.sync(self.second), 0)
        self.assertEqual(json.loads((self.installed / "manifest.json").read_text())["version"], "2")
        self.assertNotIn("enable", self.log.read_text())

    def test_a_plugin_the_user_removed_stays_removed(self):
        self.sync(self.first)
        subprocess.run(["rm", "-rf", str(self.installed)], check=True)
        self.assertEqual(self.sync(self.second), 0)
        self.assertFalse(self.installed.exists())

    def test_a_plugin_the_user_installed_is_left_alone(self):
        self.installed.mkdir(parents=True)
        (self.installed / "manifest.json").write_text('{"id": "acme.dock", "version": "mine"}')
        self.assertEqual(self.sync(self.first), 0)
        self.assertEqual(json.loads((self.installed / "manifest.json").read_text())["version"], "mine")

    def test_a_manifest_with_another_id_is_refused(self):
        self.write_manifest("evil.dock", "3")
        self.assertEqual(self.sync(git(self.repo, "rev-parse", "HEAD")), 1)
        self.assertFalse(self.installed.exists())

    def test_a_plugin_omarchy_will_not_validate_is_refused(self):
        self.write_manifest("acme.dock", "4", extra="invalid")
        self.assertEqual(self.sync(git(self.repo, "rev-parse", "HEAD")), 1)
        self.assertFalse(self.installed.exists())

    def test_a_commit_not_in_the_repository_is_refused(self):
        self.assertEqual(self.sync("0" * 40), 1)
        self.assertFalse(self.installed.exists())
        self.assertEqual(list((self.home / ".cache/umami").iterdir()), [])

    def test_dry_run_changes_nothing(self):
        self.assertEqual(self.sync(self.first, dry_run=True), 0)
        self.assertFalse(self.installed.exists())
        self.assertFalse(user.state_file().exists())

    def theme_recipe(self, theme):
        self.recipe_dir.mkdir(exist_ok=True)
        path = self.recipe_dir / "recipe.toml"
        path.write_text(f'[flavor]\nname = "test"\n\n[defaults]\ntheme = "{theme}"\n')
        return path

    def test_the_default_theme_is_set_once(self):
        self.assertEqual(user.sync(self.theme_recipe("Vantablack")), 0)
        self.assertEqual(self.log.read_text().count("theme Vantablack"), 1)
        # The user picks another theme; the next login leaves it alone.
        self.assertEqual(user.sync(self.theme_recipe("Vantablack")), 0)
        self.assertEqual(self.log.read_text().count("theme Vantablack"), 1)

    def test_a_new_default_theme_from_the_flavor_is_set(self):
        user.sync(self.theme_recipe("Vantablack"))
        user.sync(self.theme_recipe("Nord"))
        self.assertIn("theme Nord", self.log.read_text())

    def test_dry_run_sets_no_theme(self):
        self.assertEqual(user.sync(self.theme_recipe("Vantablack"), dry_run=True), 0)
        self.assertNotIn("theme", self.log.read_text())

    BAR = textwrap.dedent("""
        [flavor]
        name = "test"

        [defaults.bar]
        remove = ["omarchy.menu"]
        move = [{ id = "omarchy.clock", section = "left", index = 0 }]
        set = [{ id = "omarchy.clock", key = "format", value = "HH:mm" }]
    """)

    def test_the_bar_layout_is_set_once_with_omarchys_commands(self):
        self.recipe_dir.mkdir(exist_ok=True)
        path = self.recipe_dir / "recipe.toml"
        path.write_text(self.BAR)
        self.assertEqual(user.sync(path, shell_timeout=1), 0)
        calls = self.log.read_text().splitlines()
        bar = [c for c in calls if c.startswith(("disable", "bar"))]
        self.assertEqual(bar, ["disable omarchy.menu", "bar move omarchy.clock --section left --index 0",
                               "bar set omarchy.clock format HH:mm"])
        self.log.write_text("")
        self.assertEqual(user.sync(path, shell_timeout=1), 0)
        self.assertNotIn("bar", self.log.read_text())

    def test_bar_sections_are_checked(self):
        self.recipe_dir.mkdir(exist_ok=True)
        path = self.recipe_dir / "recipe.toml"
        path.write_text(self.BAR.replace('section = "left"', 'section = "middle"'))
        with self.assertRaises(RecipeError):
            load(path)

    def test_defaults_take_only_known_keys(self):
        self.recipe_dir.mkdir(exist_ok=True)
        path = self.recipe_dir / "recipe.toml"
        path.write_text('[flavor]\nname = "test"\n\n[defaults]\nfont = "x"\n')
        with self.assertRaises(RecipeError):
            load(path)

    def write_recipe(self, text):
        self.recipe_dir.mkdir(exist_ok=True)
        path = self.recipe_dir / "recipe.toml"
        path.write_text(text)
        return path

    def bar_calls(self):
        return [c for c in self.log.read_text().splitlines() if c.startswith(("disable", "bar"))]

    def test_a_changed_flavor_reapplies_only_the_bar_entries_it_changed(self):
        path = self.write_recipe(self.BAR)
        user.sync(path, shell_timeout=1)
        self.log.write_text("")
        # The user rearranges the bar; a new release adds one move and changes the format.
        self.write_recipe(self.BAR.replace(
            'move = [{ id = "omarchy.clock", section = "left", index = 0 }]',
            'move = [{ id = "omarchy.clock", section = "left", index = 0 }, { id = "omarchy.tray", section = "right" }]'
        ).replace('"HH:mm"', '"HH:mm:ss"'))
        self.assertEqual(user.sync(path, shell_timeout=1), 0)
        self.assertEqual(self.bar_calls(), ["bar move omarchy.tray --section right", "bar set omarchy.clock format HH:mm:ss"])

    def test_a_bar_applied_by_an_earlier_version_is_not_applied_again(self):
        path = self.write_recipe(self.BAR)
        bar = load(path).defaults["bar"]
        user.save_defaults({"test": {"bar": bar}})
        self.assertEqual(user.sync(path, shell_timeout=1), 0)
        self.assertEqual(self.bar_calls(), [])

    def test_hiding_a_widget_this_omarchy_lacks_is_not_a_failure(self):
        path = self.write_recipe(self.BAR.replace('remove = ["omarchy.menu"]', 'remove = ["omarchy.missing", "omarchy.menu"]'))
        self.assertEqual(user.sync(path, shell_timeout=1), 0)
        self.assertIn("disable omarchy.menu", self.log.read_text())

    def test_a_locked_theme_comes_back_at_every_login(self):
        path = self.write_recipe('[flavor]\nname = "test"\n\n[locked]\ntheme = "Tokyo Night"\n')
        self.assertEqual(user.sync(path), 0)
        self.assertEqual(self.log.read_text().count("theme Tokyo Night"), 1)
        user.sync(path)  # still the company theme: nothing to do
        self.assertEqual(self.log.read_text().count("theme Tokyo Night"), 1)
        Path(f"{self.log}.theme").write_text("nord\n")  # the user picks another
        user.sync(path)
        self.assertEqual(self.log.read_text().count("theme Tokyo Night"), 2)

    def test_a_locked_bar_is_applied_at_every_login(self):
        path = self.write_recipe(self.BAR.replace("[defaults.bar]", "[locked.bar]"))
        user.sync(path, shell_timeout=1)
        user.sync(path, shell_timeout=1)
        self.assertEqual(self.bar_calls().count("bar set omarchy.clock format HH:mm"), 2)

    # ------------------------------------------------------------ consent

    def test_at_login_an_undecided_plugin_waits_for_the_users_ok(self):
        self.assertEqual(self.sync(self.first, assume_yes=False), 0)
        self.assertFalse(self.installed.exists())
        for _ in range(100):  # the terminal is launched detached
            if "review" in self.log.read_text():
                break
            time.sleep(0.02)
        self.assertIn("review /usr/lib/umami/bin/umami user-sync --review", self.log.read_text())
        self.assertNotIn("enable", self.log.read_text())

    def test_saying_no_keeps_the_plugin_away(self):
        original = user.ask
        user.ask = lambda recipe, pending: False
        try:
            self.assertEqual(self.sync(self.first, assume_yes=False, interactive=True), 0)
        finally:
            user.ask = original
        self.assertFalse(self.installed.exists())
        self.log.write_text("")
        self.assertEqual(self.sync(self.second, assume_yes=False), 0)  # a later login, a new pin
        self.assertFalse(self.installed.exists())
        self.assertNotIn("review", self.log.read_text())

    def test_saying_yes_installs_it(self):
        asked = []
        original = user.ask
        user.ask = lambda recipe, pending: asked.append([p["id"] for p in pending]) or True
        try:
            self.assertEqual(self.sync(self.first, assume_yes=False, interactive=True), 0)
        finally:
            user.ask = original
        self.assertEqual(asked, [["acme.dock"]])
        self.assertTrue(self.installed.exists())

    def test_the_url_is_checked_before_cloning(self):
        evil = Path(self.tmp.name) / "evil-dock"
        subprocess.run(["cp", "-r", str(self.repo), str(evil)], check=True)
        path = self.recipe(self.first)
        path.write_text(path.read_text().replace(str(self.repo), str(evil)))
        self.assertEqual(user.sync(path, shell_timeout=1, assume_yes=True), 1)
        self.assertFalse(self.installed.exists())
        self.assertIn("urlcheck", self.log.read_text())

    def test_the_bar_layout_waits_for_the_plugins_it_places(self):
        self.recipe_dir.mkdir(exist_ok=True)
        path = self.recipe_dir / "recipe.toml"
        path.write_text(self.recipe(self.first).read_text() + textwrap.dedent("""
            [defaults.bar]
            move = [{ id = "acme.dock", section = "right", index = 99 }]
        """))
        self.assertEqual(user.sync(path, shell_timeout=1), 0)
        self.assertNotIn("bar move", self.log.read_text())
        self.assertEqual(user.sync(path, shell_timeout=1, assume_yes=True), 0)
        self.assertIn("bar move acme.dock --section right --index 99", self.log.read_text())

    def test_the_pin_must_be_a_full_sha(self):
        with self.assertRaises(RecipeError):
            load(self.recipe(self.first[:12]))


if __name__ == "__main__":
    unittest.main()
