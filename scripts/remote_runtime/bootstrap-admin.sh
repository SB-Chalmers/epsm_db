#!/usr/bin/env bash
set -euo pipefail
test "$(id -un)" = csd-lab || { echo 'Run this as csd-lab'; exit 1; }
sudo -v
sudo apt-get update
sudo apt-get install -y ca-certificates curl python3.12-venv
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
source /etc/os-release
test "$ID" = ubuntu && test "$VERSION_CODENAME" = noble
architecture="$(dpkg --print-architecture)"
printf '%s\n' "deb [arch=$architecture signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" | sudo tee /etc/apt/sources.list.d/ubem-docker.list >/dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker csd-lab
sudo loginctl enable-linger csd-lab
# Existing user manager sessions do not gain new groups until they restart.
echo 'Administrator setup complete. Log out and back in (or reboot), then run:'
echo 'systemctl --user restart ubem-remote.service'
