import contextlib
import io
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
    "omarchy-notification-send": 'echo "notify $*" >> "$FAKE_LOG"',
    "omarchy-theme-set": 'echo "theme $*" >> "$FAKE_LOG"; echo "$*" > "$FAKE_LOG.theme"',
    "omarchy-theme-current": 'cat "$FAKE_LOG.theme" 2>/dev/null || echo Unknown',
    "omarchy-bar": 'echo "bar $*" >> "$FAKE_LOG"; [[ $2 != *gone* ]] || { echo "omarchy-bar: could not find widget $2" >&2; exit 1; }',
    "omarchy-plugin-remove": 'echo "remove $*" >> "$FAKE_LOG"; rm -rf "$HOME/.config/omarchy/plugins/$1"',
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
        add = [{ id = "omarchy.tailscale", section = "right", index = 0 }]
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
    self.assertEqual(bar, ["disable omarchy.menu", "bar put omarchy.tailscale --section right --index 0",
                           "bar move omarchy.clock --section left --index 0", "bar set omarchy.clock format HH:mm"])
    self.log.write_text("")
    self.assertEqual(user.sync(path, shell_timeout=1), 0)
    self.assertNotIn("bar", self.log.read_text())

  def test_shell_calls_wait_longer_than_omarchy_shells_default(self):
    # Just after a shell restart, a call can take more than the 2s
    # omarchy-shell allows by default although it runs.
    for name in ("omarchy-plugin-disable", "omarchy-bar"):
      fake = Path(self.tmp.name) / "bin" / name
      fake.write_text('#!/bin/bash\n[[ $OMARCHY_SHELL_IPC_TIMEOUT == 30s ]] || '
                      '{ echo "omarchy-shell is not responding" >&2; exit 1; }\n')
    self.recipe_dir.mkdir(exist_ok=True)
    path = self.recipe_dir / "recipe.toml"
    path.write_text(self.BAR)
    self.assertEqual(user.sync(path, shell_timeout=1), 0)

  def test_a_widget_not_on_your_bar_is_skipped_and_the_rest_applies(self):
    self.recipe_dir.mkdir(exist_ok=True)
    path = self.recipe_dir / "recipe.toml"
    path.write_text('''
            [flavor]
            name = "t"
            [defaults.bar]
            move = [
              { id = "acme.gone", section = "center", index = 0 },
              { id = "omarchy.clock", section = "left", index = 0 },
            ]
        '''.replace("\n            ", "\n"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
      self.assertEqual(user.sync(path, shell_timeout=1), 0)
    self.assertIn("bar move omarchy.clock --section left --index 0", self.log.read_text())
    self.assertIn("skipped   move acme.gone: not on your bar", out.getvalue())
    # Done once, like the rest of the defaults: not tried again at every login.
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

  # ---------------------------------------------------------- switching

  def switch_to_a_flavor_without_the_dock(self):
    self.sync(self.first)  # flavor "test" installs acme.dock
    mine = self.home / ".config/omarchy/plugins/me.mine"
    mine.mkdir(parents=True)
    return self.write_recipe('[flavor]\nname = "other"\n'), mine

  def answering(self, answer, path, **kwargs):
    asked = []
    original = user.ask_leftovers
    user.ask_leftovers = lambda recipe, state, ids: asked.append(ids) or answer
    try:
      self.assertEqual(user.sync(path, shell_timeout=1, **kwargs), 0)
    finally:
      user.ask_leftovers = original
    return asked

  def test_switching_flavors_offers_to_remove_the_old_flavors_plugins(self):
    path, mine = self.switch_to_a_flavor_without_the_dock()
    self.assertEqual(self.answering(True, path, interactive=True), [["acme.dock"]])
    self.assertFalse(self.installed.exists())
    self.assertTrue(mine.exists())
    self.assertIn("remove acme.dock --yes", self.log.read_text())
    self.assertNotIn("acme.dock", user.load_state())

  def test_kept_leftovers_become_yours_and_are_not_asked_about_again(self):
    path, _ = self.switch_to_a_flavor_without_the_dock()
    self.answering(False, path, interactive=True)
    self.assertTrue(self.installed.exists())
    self.assertEqual(self.answering(True, path, interactive=True), [])

  def test_at_login_leftovers_open_the_review_and_nothing_is_removed(self):
    path, _ = self.switch_to_a_flavor_without_the_dock()
    self.log.write_text("")
    self.assertEqual(self.answering(True, path), [])
    self.assertTrue(self.installed.exists())
    for _ in range(100):  # the terminal is launched detached
      if "review" in self.log.read_text():
        break
      time.sleep(0.02)
    self.assertIn("user-sync --review", self.log.read_text())

  def test_a_plugin_the_new_flavor_also_has_is_not_a_leftover(self):
    self.sync(self.first)
    state = user.load_state()
    state["acme.dock"]["flavor"] = "earlier"
    user.save_state(state)
    self.assertEqual(self.answering(True, self.recipe(self.first), interactive=True), [])

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
    user.ask = lambda recipe, pending, suites=(): False
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
    user.ask = lambda recipe, pending, suites=(): asked.append([p["id"] for p in pending]) or True
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


class SuiteTest(unittest.TestCase):
  """[[suite]]: a package's own plugin installer, run as the user."""

  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    root = Path(self.tmp.name)
    self.home = root / "home"
    self.home.mkdir()
    self.log = root / "calls.log"
    self.log.touch()
    self.version = root / "version"
    self.version.write_text("0.2.4-1")
    self.owner = root / "owner"
    self.owner.write_text("shibumi-shell")
    fakes, self.pkgbin = root / "bin", root / "usr-bin"
    fakes.mkdir()
    self.pkgbin.mkdir()
    bodies = dict(FAKES)
    bodies["pacman"] = ('case $1 in -Q) [[ -s "$FAKE_VERSION" ]] && echo "$3 $(cat "$FAKE_VERSION")" || exit 1 ;; '
                        '-Qqo) cat "$FAKE_OWNER" ;; esac')
    for name, body in bodies.items():
      (fakes / name).write_text("#!/bin/bash\n" + body + "\n")
      (fakes / name).chmod(0o755)
    suite = self.pkgbin / "shibumi-shell"
    suite.write_text('#!/bin/bash\necho "suite $* timeout=$OMARCHY_SHELL_IPC_TIMEOUT" >> "$FAKE_LOG"\n')
    suite.chmod(0o755)
    self.saved_env, self.saved_path = dict(os.environ), user.SUITE_PATH
    user.SUITE_PATH = str(self.pkgbin)
    os.environ.update({"HOME": str(self.home), "PATH": f"{fakes}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
                       "FAKE_VERSION": str(self.version), "FAKE_OWNER": str(self.owner)})
    for key in ("XDG_STATE_HOME", "XDG_CACHE_HOME"):
      os.environ.pop(key, None)

  def tearDown(self):
    os.environ.clear()
    os.environ.update(self.saved_env)
    user.SUITE_PATH = self.saved_path
    self.tmp.cleanup()

  def recipe(self, name="shibumi", suite=True):
    path = Path(self.tmp.name) / name / "recipe.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f'''
[flavor]
name = "{name}"
''' + ('''
[[package]]
name = "shibumi-shell"

[[suite]]
id = "shibumi"
package = "shibumi-shell"
command = "shibumi-shell"
install = ["install", "--yes"]
update = ["update", "--yes"]
uninstall = ["uninstall", "--yes"]
''' if suite else '''
[defaults]
theme = "Solitude"
'''))
    return path

  def calls(self):
    return [c for c in self.log.read_text().splitlines() if c.startswith("suite")]

  def test_installs_once_then_updates_when_the_package_changes(self):
    self.assertEqual(user.sync(self.recipe(), shell_timeout=1, assume_yes=True), 0)
    self.assertEqual(self.calls(), ["suite install --yes timeout=30s"])
    self.log.write_text("")
    self.assertEqual(user.sync(self.recipe(), shell_timeout=1), 0)  # a later login
    self.assertEqual(self.calls(), [])
    self.version.write_text("0.2.5-1")  # omarchy update brought a new package
    self.assertEqual(user.sync(self.recipe(), shell_timeout=1), 0)
    self.assertEqual(self.calls(), ["suite update --yes timeout=30s"])

  def test_nothing_runs_before_the_user_says_yes(self):
    self.assertEqual(user.sync(self.recipe(), shell_timeout=1), 0)
    self.assertEqual(self.calls(), [])
    self.assertIn("review /usr/lib/umami/bin/umami user-sync --review", self.log.read_text())

  def test_saying_no_keeps_it_away(self):
    original = user.ask
    user.ask = lambda recipe, pending, suites=(): False
    try:
      self.assertEqual(user.sync(self.recipe(), shell_timeout=1, interactive=True), 0)
    finally:
      user.ask = original
    self.version.write_text("0.2.5-1")
    self.assertEqual(user.sync(self.recipe(), shell_timeout=1), 0)
    self.assertEqual(self.calls(), [])

  def test_a_command_its_package_does_not_own_is_not_run(self):
    self.owner.write_text("something-else")
    self.assertEqual(user.sync(self.recipe(), shell_timeout=1, assume_yes=True), 1)
    self.assertEqual(self.calls(), [])

  def test_a_missing_package_is_reported(self):
    self.version.write_text("")
    self.assertEqual(user.sync(self.recipe(), shell_timeout=1, assume_yes=True), 1)
    self.assertEqual(self.calls(), [])

  def test_the_removed_suite_is_named_plainly(self):
    state = {"suite:shibumi": {"package": "shibumi-shell", "version": "1", "uninstall": ["uninstall", "--yes"],
                               "command": str(self.pkgbin / "shibumi-shell"), "flavor": "shibumi"}}
    original = user.ask_leftovers
    user.ask_leftovers = lambda recipe, state, ids: True
    try:
      lines = user.tidy(user.load(self.recipe("zen", suite=False)), state, ["suite:shibumi"],
                        dry_run=False, interactive=True, assume_yes=False)
    finally:
      user.ask_leftovers = original
    self.assertEqual(lines, ["  removed   shibumi (the shibumi-shell suite)"])

  def test_a_flavor_without_it_offers_its_uninstall(self):
    self.assertEqual(user.sync(self.recipe(), shell_timeout=1, assume_yes=True), 0)
    self.log.write_text("")
    asked, original = [], user.ask_leftovers
    user.ask_leftovers = lambda recipe, state, ids: asked.append(ids) or True
    try:
      self.assertEqual(user.sync(self.recipe("zen", suite=False), shell_timeout=1, interactive=True), 0)
    finally:
      user.ask_leftovers = original
    self.assertEqual(asked, [["suite:shibumi"]])
    self.assertEqual(self.calls(), ["suite uninstall --yes timeout=30s"])
    self.assertNotIn("suite:shibumi", user.load_state())
