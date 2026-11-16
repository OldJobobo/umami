#!/bin/bash

# Remove omarchy-flavor. Omarchy runs stock from the next boot; the composed
# tree stays until then, because the running session still uses it.

set -euo pipefail

if [[ -x /usr/lib/omarchy-flavor/bin/omarchy-flavor ]]; then
  sudo /usr/lib/omarchy-flavor/bin/omarchy-flavor disable
fi
sudo rm -f /etc/pacman.d/hooks/90-omarchy-flavor.hook
sudo git config --system --unset-all safe.directory /var/lib/omarchy-flavor/tree 2>/dev/null || true
sudo rm -rf /usr/lib/omarchy-flavor /etc/omarchy-flavor
echo "Reboot, then remove the old tree: sudo rm -rf /var/lib/omarchy-flavor"
