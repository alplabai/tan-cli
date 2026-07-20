#!/usr/bin/env sh
# SPDX-License-Identifier: Apache-2.0
#
# tan installer for Linux + macOS. Downloads the prebuilt `tan` binary for this
# platform from GitHub Releases and installs it. By DEFAULT it installs to a
# user-local dir (~/.local/bin) so NO sudo/admin is needed. Pass --system to
# install to /usr/local/bin (that path needs elevated permission -> uses sudo).
#
#   curl -fsSL https://raw.githubusercontent.com/alplabai/tan-cli/main/install.sh | sh
#   ./install.sh [--version vX.Y.Z] [--dir <path>] [--system]
set -eu

REPO="alplabai/tan-cli"
VERSION="latest"
INSTALL_DIR="${TAN_INSTALL_DIR:-$HOME/.local/bin}"

while [ $# -gt 0 ]; do
	case "$1" in
	--version) VERSION="${2:?--version needs a value}"; shift 2 ;;
	--dir) INSTALL_DIR="${2:?--dir needs a value}"; shift 2 ;;
	--system) INSTALL_DIR="/usr/local/bin"; shift ;;
	-h | --help)
		echo "usage: install.sh [--version vX.Y.Z] [--dir <path>] [--system]"
		echo "  default install dir: \$HOME/.local/bin (no sudo). --system uses /usr/local/bin (sudo)."
		exit 0
		;;
	*) echo "install.sh: unknown argument: $1" >&2; exit 2 ;;
	esac
done

# host os -> rust target os part
os="$(uname -s)"
case "$os" in
Darwin) os_part="apple-darwin" ;;
Linux) os_part="unknown-linux-gnu" ;;
*) echo "install.sh: unsupported OS '$os' — on Windows use install.ps1" >&2; exit 1 ;;
esac

# host arch -> rust target arch part
arch="$(uname -m)"
case "$arch" in
arm64 | aarch64) arch_part="aarch64" ;;
x86_64 | amd64) arch_part="x86_64" ;;
*) echo "install.sh: unsupported architecture '$arch'" >&2; exit 1 ;;
esac

asset="tan-${arch_part}-${os_part}"
if [ "$VERSION" = "latest" ]; then
	url="https://github.com/${REPO}/releases/latest/download/${asset}"
else
	url="https://github.com/${REPO}/releases/download/${VERSION}/${asset}"
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
echo "install.sh: downloading tan (${arch_part}-${os_part}, ${VERSION})…"
if command -v curl >/dev/null 2>&1; then
	curl -fSL --proto '=https' --tlsv1.2 -o "$tmp" "$url"
elif command -v wget >/dev/null 2>&1; then
	wget -qO "$tmp" "$url"
else
	echo "install.sh: need curl or wget on PATH" >&2
	exit 1
fi
chmod +x "$tmp"

dest="${INSTALL_DIR}/tan"
# User-local dir: create + move without sudo. A non-writable dir (e.g. the
# --system /usr/local/bin) needs elevated permission -> use sudo explicitly so
# the admin step is visible, never silent.
if mkdir -p "$INSTALL_DIR" 2>/dev/null && [ -w "$INSTALL_DIR" ]; then
	mv "$tmp" "$dest"
else
	echo "install.sh: ${INSTALL_DIR} needs elevated permission — running sudo (admin)."
	sudo mkdir -p "$INSTALL_DIR"
	sudo mv "$tmp" "$dest"
	sudo chmod +x "$dest"
fi
trap - EXIT

echo "install.sh: installed tan -> ${dest}"
case ":${PATH}:" in
*":${INSTALL_DIR}:"*) ;;
*) echo "install.sh: ${INSTALL_DIR} is not on PATH — add:  export PATH=\"${INSTALL_DIR}:\$PATH\"" ;;
esac
"$dest" --version 2>/dev/null || echo "install.sh: run 'tan --version' to verify (once on PATH)."
