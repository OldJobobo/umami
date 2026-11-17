import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from omarchy_flavor import user
from omarchy_flavor.recipe import RecipeError, load

# Stand-ins for Omarchy's commands: each logs its arguments, and the validator
# refuses a plugin whose manifest says "invalid".
FAKES = {
    "omarchy-plugin-validate": 'echo "validate $*" >> "$FAKE_LOG"; ! grep -q invalid "$1/manifest.json"',
    "omarchy-plugin-enable": 'echo "enable $*" >> "$FAKE_LOG"',
    "omarchy-plugin-list": 'echo "[{\\"id\\": \\"acme.dock\\"}]"',
    "omarchy-shell": 'echo "shell $*" >> "$FAKE_LOG"',
    "notify-send": 'echo "notify $*" >> "$FAKE_LOG"',
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
        self.assertEqual(list((self.home / ".cache/omarchy-flavor").iterdir()), [])

    def test_dry_run_changes_nothing(self):
        self.assertEqual(self.sync(self.first, dry_run=True), 0)
        self.assertFalse(self.installed.exists())
        self.assertFalse(user.state_file().exists())

    def test_the_pin_must_be_a_full_sha(self):
        with self.assertRaises(RecipeError):
            load(self.recipe(self.first[:12]))


if __name__ == "__main__":
    unittest.main()
