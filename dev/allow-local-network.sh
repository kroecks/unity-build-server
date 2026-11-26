#!/bin/bash
# Grant a machine-wide Local Network exemption so the launchd-managed build
# server can reach LAN hosts (your Gitea NAS, devices, etc.).
#
# WHY THIS IS NEEDED: on macOS Sequoia+ (15.x), "Local Network Privacy" blocks
# LAN connections from processes launched by launchd (our LaunchAgent). Processes
# started from Terminal/SSH are exempt, which is why the old nohup server worked
# and the launchd one gets "Couldn't connect to server" in ~1ms when cloning from
# the NAS. This applies a subnet exemption for ALL processes (TN3179, macOS 15.5+),
# which is the right fix for a dedicated build box.
#
#   sudo ./dev/allow-local-network.sh                 # defaults to 192.168.1.0/24
#   sudo ./dev/allow-local-network.sh 10.0.0.0/24     # or pass your own subnet
#
# A REBOOT is required afterward for it to take effect.
set -euo pipefail

if [[ "$(id -u)" != "0" ]]; then
  echo "Run with sudo:  sudo $0 [subnet-CIDR]" >&2
  exit 1
fi

SUBNET="${1:-192.168.1.0/24}"

# Sanity: must be macOS 15.5+ for this key to work.
VER="$(sw_vers -productVersion 2>/dev/null || echo 0)"
echo "macOS version: $VER"
MAJOR="${VER%%.*}"
REST="${VER#*.}"; MINOR="${REST%%.*}"; [[ "$MINOR" == "$VER" ]] && MINOR=0
if (( MAJOR < 15 )) || { (( MAJOR == 15 )) && (( MINOR < 5 )); }; then
  echo "WARNING: the subnet-exemption key needs macOS 15.5 or newer." >&2
  echo "  On older macOS this won't take effect. If the clone still fails after" >&2
  echo "  a reboot, tell Claude your version and use the signed-.app wrapper instead." >&2
fi

echo "Granting Local Network exemption for subnet: $SUBNET"

# Apply to both Ethernet and Wi-Fi interfaces (covers whichever the mini uses).
defaults write com.apple.network.local-network \
  AllowedEthernetLocalNetworkAddresses -array "$SUBNET"
defaults write com.apple.network.local-network \
  AllowedWiFiLocalNetworkAddresses -array "$SUBNET"

echo
echo "--- Verifying written values ---"
defaults read com.apple.network.local-network 2>/dev/null || echo "(could not read back)"

cat <<EOF

────────────────────────────────────────────────────────────────────────
Done writing the exemption. It only loads at boot, so:

    sudo shutdown -r now

After the Mac comes back (auto-login will start the build server), retry the
onboard. The clone from the NAS should connect.

This reboot also confirms your launchd + auto-login setup survives a reboot.
────────────────────────────────────────────────────────────────────────
EOF
