#!/bin/bash

# Remove umami. Omarchy runs stock from the next boot; the composed
# tree stays until then, because the running session still uses it.

set -euo pipefail

if [[ -x /usr/lib/umami/bin/umami ]]; then
  sudo /usr/lib/umami/bin/umami disable
fi
sudo systemctl disable --now umami-users.path 2>/dev/null || true
sudo rm -f /etc/pacman.d/hooks/90-umami.hook /etc/xdg/autostart/umami.desktop \
  /etc/systemd/system/umami-users.path /etc/systemd/system/umami-users.service
sudo git config --system --unset-all safe.directory /var/lib/umami/tree 2>/dev/null || true
sudo rm -rf /usr/lib/umami /etc/umami
echo "Reboot, then remove the old tree: sudo rm -rf /var/lib/umami"
