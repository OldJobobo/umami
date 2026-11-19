#!/bin/bash

# Install umami and a recipe on this machine (prototype installer).
#
#   ./install.sh examples/zen
#
# Copies the compositor to /usr/lib/umami, the recipe directory to
# /etc/umami, the pacman hook to /etc/pacman.d/hooks, composes the
# tree, and points Omarchy at it from the next boot.

set -euo pipefail

src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
recipe_dir=${1:-}

if [[ -z $recipe_dir || ! -f $recipe_dir/recipe.toml ]]; then
  echo "Usage: ./install.sh <recipe-dir containing recipe.toml>" >&2
  exit 1
fi

if [[ -f /etc/omarchy.conf ]] && grep -q '^export OMARCHY_PATH=' /etc/omarchy.conf && ! grep -q 'umami' /etc/omarchy.conf; then
  current=$(. /etc/omarchy.conf; echo "$OMARCHY_PATH")
  if [[ $current != "/usr/share/omarchy" ]]; then
    echo "Omarchy is dev-linked to $current. Run omarchy dev unlink first." >&2
    exit 1
  fi
fi

sudo install -d -m 755 /usr/lib/umami/bin /usr/lib/umami/umami
sudo install -m 755 "$src/bin/umami" /usr/lib/umami/bin/
sudo install -m 644 "$src"/umami/*.py /usr/lib/umami/umami/

sudo rm -rf /etc/umami.new
sudo cp -r "$recipe_dir" /etc/umami.new
sudo chown -R root:root /etc/umami.new
sudo chmod -R go-w /etc/umami.new
sudo rm -rf /etc/umami
sudo mv /etc/umami.new /etc/umami

sudo install -D -m 644 "$src/system/90-umami.hook" /etc/pacman.d/hooks/90-umami.hook

sudo /usr/lib/umami/bin/umami recompose
sudo /usr/lib/umami/bin/umami enable
echo "Reboot to start running the flavor."
