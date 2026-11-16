#!/bin/bash

# Install omarchy-flavor and a recipe on this machine (prototype installer).
#
#   ./install.sh examples/jobo
#
# Copies the compositor to /usr/lib/omarchy-flavor, the recipe directory to
# /etc/omarchy-flavor, the pacman hook to /etc/pacman.d/hooks, composes the
# tree, and points Omarchy at it from the next boot.

set -euo pipefail

src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
recipe_dir=${1:-}

if [[ -z $recipe_dir || ! -f $recipe_dir/recipe.toml ]]; then
  echo "Usage: ./install.sh <recipe-dir containing recipe.toml>" >&2
  exit 1
fi

if [[ -f /etc/omarchy.conf ]] && grep -q '^export OMARCHY_PATH=' /etc/omarchy.conf && ! grep -q 'omarchy-flavor' /etc/omarchy.conf; then
  current=$(. /etc/omarchy.conf; echo "$OMARCHY_PATH")
  if [[ $current != "/usr/share/omarchy" ]]; then
    echo "Omarchy is dev-linked to $current. Run omarchy dev unlink first." >&2
    exit 1
  fi
fi

sudo install -d -m 755 /usr/lib/omarchy-flavor/bin /usr/lib/omarchy-flavor/omarchy_flavor
sudo install -m 755 "$src/bin/omarchy-flavor" /usr/lib/omarchy-flavor/bin/
sudo install -m 644 "$src"/omarchy_flavor/*.py /usr/lib/omarchy-flavor/omarchy_flavor/

sudo rm -rf /etc/omarchy-flavor.new
sudo cp -r "$recipe_dir" /etc/omarchy-flavor.new
sudo chown -R root:root /etc/omarchy-flavor.new
sudo chmod -R go-w /etc/omarchy-flavor.new
sudo rm -rf /etc/omarchy-flavor
sudo mv /etc/omarchy-flavor.new /etc/omarchy-flavor

sudo install -D -m 644 "$src/system/90-omarchy-flavor.hook" /etc/pacman.d/hooks/90-omarchy-flavor.hook

sudo /usr/lib/omarchy-flavor/bin/omarchy-flavor recompose
sudo /usr/lib/omarchy-flavor/bin/omarchy-flavor enable
echo "Reboot to start running the flavor."
