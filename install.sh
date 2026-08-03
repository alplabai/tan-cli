#!/usr/bin/env sh
# SPDX-License-Identifier: Apache-2.0
#
# tan installer for Linux + macOS. Downloads the prebuilt `tan` release
# archive for this platform from GitHub Releases, unpacks it, and installs a
# launcher. By DEFAULT it installs to a user-local dir (~/.local/bin) so NO
# sudo/admin is needed. Pass --system to install to /usr/local/bin (that path
# needs elevated permission -> uses sudo).
#
# From v0.5.0-rc4 (tan-cli#349) the asset is a PyInstaller --onedir freeze
# archived as a .tar.gz, not a raw executable: --onefile re-extracted its
# whole runtime into a fresh temp dir on EVERY invocation, which measured
# 13-19 s on macOS (unsigned re-extracted .dylibs get re-verified by the OS on
# every load). $INSTALL_DIR/tan is therefore a thin launcher script now, not
# the binary itself -- the unpacked freeze lives in $INSTALL_DIR/tan-cli-lib/.
#
#   curl -fsSL https://raw.githubusercontent.com/alplabai/tan-cli/main/install.sh | sh
#   ./install.sh [--version vX.Y.Z] [--dir <path>] [--system]
set -eu

REPO="alplabai/tan-cli"
VERSION="latest"
INSTALL_DIR="${TAN_INSTALL_DIR:-$HOME/.local/bin}"
MODIFY_PATH=1

while [ $# -gt 0 ]; do
	case "$1" in
	--version) VERSION="${2:?--version needs a value}"; shift 2 ;;
	--dir) INSTALL_DIR="${2:?--dir needs a value}"; shift 2 ;;
	--system) INSTALL_DIR="/usr/local/bin"; shift ;;
	--no-modify-path) MODIFY_PATH=0; shift ;;
	-h | --help)
		echo "usage: install.sh [--version vX.Y.Z] [--dir <path>] [--system] [--no-modify-path]"
		echo "  default install dir: \$HOME/.local/bin (no sudo). --system uses /usr/local/bin (sudo)."
		echo "  if the install dir is not already on PATH, the login shell's rc file is updated"
		echo "  (with a printed notice) so 'tan' works in any shell; --no-modify-path opts out."
		exit 0
		;;
	*) echo "install.sh: unknown argument: $1" >&2; exit 2 ;;
	esac
done

# host os -> rust target os part
os="$(uname -s)"
case "$os" in
Darwin) os_part="apple-darwin" ;;
# gnu, NOT musl. From v0.5.0 the binary is a PyInstaller freeze of the Python
# port, and PyInstaller cannot produce the "static, runs on any libc" artefact
# the Rust -musl target did: a musl freeze is dynamically linked against
# /lib/ld-musl-x86_64.so.1 and runs ONLY on musl distros. So the Linux asset is
# built on Debian 11 and named -gnu, and requesting -musl here would 404 on
# every v0.5.0+ tag. Older (Rust) releases published BOTH, so this also
# resolves for them -- with the Rust build's measured GLIBC_2.30 floor
# (readelf -V on the shipped binary; its cargo-zigbuild pin target was a
# different number, 2.31 -- see CHANGELOG 0.4.0, which retracts pairing
# "2.31 floor" with the pre-fix "GLIBC_2.39 not found" symptom as wrong on
# both numbers).
Linux) os_part="unknown-linux-gnu" ;;
*) echo "install.sh: unsupported OS '$os' -- on Windows use install.ps1" >&2; exit 1 ;;
esac

# musl hosts (Alpine and similar) cannot run the -gnu binary above AT ALL --
# not a checksum failure, a bare "not found" from the shell AFTER the sha256
# verify below already passed, so none of that section's four refusals ever
# fires and the script reports success. Catch it here instead, before any
# download: `ldd --version` names musl on the first line where glibc's ldd
# names itself; some minimal images have no ldd at all, so also check for the
# musl dynamic loader directly.
if [ "$os_part" = "unknown-linux-gnu" ]; then
	is_musl=0
	if command -v ldd >/dev/null 2>&1; then
		if ldd --version 2>&1 | grep -qi musl; then
			is_musl=1
		fi
	elif ls /lib/ld-musl-*.so.1 >/dev/null 2>&1; then
		# Only reached when there is no ldd to ask. The loader-file probe alone
		# is NOT sufficient on its own: Debian/Ubuntu's musl package (pulled in
		# by musl-tools, which any host that ever cross-built the -musl target
		# has) installs /lib/ld-musl-x86_64.so.1 on an otherwise glibc host, and
		# that host's ldd correctly reports glibc -- so it must never be gated
		# out by falling through here.
		is_musl=1
	fi
	if [ "$is_musl" = "1" ]; then
		echo "install.sh: this host's libc is musl (e.g. Alpine) -- no Linux asset is published for it. From v0.5.0 the binary is a PyInstaller freeze, which cannot produce the static musl artefact older Rust releases did; the only Linux asset now is -unknown-linux-gnu, and it cannot exec on a musl host." >&2
		echo "install.sh: refusing to install. Install from a checkout instead: git clone https://github.com/${REPO} && pip install ./tan-cli/python" >&2
		exit 1
	fi
fi

# host arch -> rust target arch part
arch="$(uname -m)"
case "$arch" in
arm64 | aarch64) arch_part="aarch64" ;;
x86_64 | amd64) arch_part="x86_64" ;;
*) echo "install.sh: unsupported architecture '$arch'" >&2; exit 1 ;;
esac

# .tar.gz: install.sh only ever targets Linux/macOS (Windows uses install.ps1,
# whose asset is the .zip build_binary.sh produces for that OS instead).
asset="tan-${arch_part}-${os_part}.tar.gz"

# One HTTP download, curl or wget, quiet about nothing. Both branches keep the
# flags they had inline before (`--proto '=https' --tlsv1.2`, and no -q on
# wget) -- the transport's own error IS the primary diagnostic, so it is never
# swallowed in favour of a guess.
download() { # $1 = url, $2 = output path; returns non-zero on failure
	if command -v curl >/dev/null 2>&1; then
		curl -fSL --proto '=https' --tlsv1.2 -o "$2" "$1"
	elif command -v wget >/dev/null 2>&1; then
		wget -O "$2" "$1"
	else
		echo "install.sh: need curl or wget on PATH" >&2
		exit 1
	fi
}

# `latest` is a REDIRECT, and resolving it twice is not the same as resolving
# it once: a release cut between the binary fetch and the checksums fetch would
# have us verify one release's asset against another release's digests. Rare,
# silent, and it produces a WRONG VERDICT rather than an error -- so pin the tag
# up front and build both URLs from it.
#
# The digest for a given filename really does move between tags: at v0.4.0-rc1
# `tan-x86_64-pc-windows-msvc.exe` is f159c1dc..., at v0.4.0 it is a80fb5da...,
# same asset name. Anything that caches or hardcodes a digest is wrong by
# construction; the digest must come from the tag the binary came from.
if [ "$VERSION" = "latest" ]; then
	echo "install.sh: resolving the latest release tag..."
	if command -v curl >/dev/null 2>&1; then
		resolved="$(curl -fsSL --proto '=https' --tlsv1.2 -o /dev/null \
			-w '%{url_effective}' "https://github.com/${REPO}/releases/latest" 2>/dev/null |
			sed -n 's#.*/releases/tag/##p')"
	else
		resolved="$(wget -S --spider --max-redirect=20 \
			"https://github.com/${REPO}/releases/latest" 2>&1 |
			sed -n 's#^[[:space:]]*Location:.*/releases/tag/\([^[:space:]]*\).*#\1#p' | tail -1)"
	fi
	if [ -z "${resolved:-}" ]; then
		echo "install.sh: could not resolve which release 'latest' points at." >&2
		echo "install.sh: refusing to install -- without a tag there is no checksums.txt to verify against. Retry, or pass an explicit --version vX.Y.Z." >&2
		exit 1
	fi
	VERSION="$resolved"
	echo "install.sh: latest is ${VERSION}."
fi
url="https://github.com/${REPO}/releases/download/${VERSION}/${asset}"
sums_url="https://github.com/${REPO}/releases/download/${VERSION}/checksums.txt"

tmp="$(mktemp)"
sums="$(mktemp)"
stage="$(mktemp -d)"
trap 'rm -f "$tmp" "$sums"; rm -rf "$stage"' EXIT
echo "install.sh: downloading tan (${arch_part}-${os_part}, ${VERSION})..."
dl_ok=1
download "$url" "$tmp" || dl_ok=0
if [ "$dl_ok" = "0" ]; then
	echo "install.sh: download failed: ${url}" >&2
	# The transport error above says THAT it failed, never why, and a 404 for
	# an asset that was never published looks identical to a proxy outage. Name
	# the causes this script can actually know; guess at nothing else.
	case "${arch_part}-${os_part}" in
	aarch64-unknown-linux-gnu)
		echo "install.sh: note -- there is no prebuilt Linux arm64 asset from v0.5.0 onward. The binary is a frozen build that must be produced on the architecture it runs on, and the release builds no arm64 Linux. Install from a checkout instead: git clone https://github.com/${REPO} && pip install ./tan-cli/python" >&2
		;;
	esac
	echo "install.sh: if this is a 404 rather than a network failure, check which assets ${VERSION} actually publishes: https://github.com/${REPO}/releases" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Verify what landed against the checksums.txt published in the SAME release.
#
# TLS says we talked to github.com. It does not say github.com handed us the
# bytes we published, and it says nothing at all about a proxy, a cache, or a
# truncated write. `checksums.txt` is the artefact that does, it already exists
# at every tag, and alp-sdk-vscode already verifies its own managed download
# against it (alplabai/alp-sdk-vscode#389) and refuses a mismatch. Until this
# landed, the two acquisition paths for the same binary disagreed about whether
# they check it -- and the unverified one is the path the extension's
# "Install tan CLI (global)" button runs, whose result the extension's resolver
# then PREFERS over its own verified copy, on every activation, indefinitely.
#
# FOUR distinct outcomes, four distinct messages, all of them refusing. They are
# different facts and a user must not have to guess which one they hit: being
# offline behind a corporate proxy and being handed a tampered binary are not
# the same situation and must not read the same. (#389 reached the same shape
# from the other side.) Nothing is written to the install dir on any of them --
# the downloaded archive is still in $tmp here and the trap removes it.
# ---------------------------------------------------------------------------
if command -v sha256sum >/dev/null 2>&1; then
	got="$(sha256sum "$tmp" | cut -d' ' -f1)"
elif command -v shasum >/dev/null 2>&1; then
	got="$(shasum -a 256 "$tmp" | cut -d' ' -f1)"
else
	# Outcome 4. Deliberately NOT a warn-and-continue, and deliberately no
	# --no-verify escape hatch: a flag that turns the check off is the hole
	# this closes, wearing a consent form. coreutils, busybox and macOS all
	# ship one of these two, so reaching here means a genuinely unusual host.
	echo "install.sh: cannot verify -- neither sha256sum nor shasum is on PATH." >&2
	echo "install.sh: refusing to install an unverified binary. Install coreutils (sha256sum) or perl/shasum, then re-run." >&2
	exit 1
fi

echo "install.sh: verifying against ${VERSION} checksums.txt..."
if ! download "$sums_url" "$sums" 2>/dev/null; then
	# Outcome 1: the digests could not be fetched. Says nothing about the
	# binary -- which is exactly why it must not be worded like a mismatch.
	echo "install.sh: could not fetch ${sums_url}" >&2
	echo "install.sh: refusing to install -- the binary downloaded, but there is nothing to check it against. This is a fetch failure, NOT evidence the binary is bad. Retry, or check a proxy/firewall." >&2
	exit 1
fi

want="$(awk -v a="$asset" '$2 == a { print $1 }' "$sums" | head -1)"
if [ -z "${want:-}" ]; then
	# Outcome 2: fetched fine, but this asset is not in it. A release that
	# shipped the binary and omitted it from checksums.txt is a release bug,
	# and silently installing anyway is how it would stay one.
	echo "install.sh: ${asset} is not listed in ${VERSION}'s checksums.txt" >&2
	echo "install.sh: refusing to install -- the digest file exists but does not cover this asset, so it cannot be verified. Report this against ${REPO}; the release is incomplete." >&2
	exit 1
fi

if [ "$got" != "$want" ]; then
	# Outcome 3: the one that means something is actually wrong.
	echo "install.sh: SHA256 MISMATCH for ${asset} (${VERSION})" >&2
	echo "install.sh:   expected ${want}" >&2
	echo "install.sh:   got      ${got}" >&2
	echo "install.sh: refusing to install. The downloaded bytes are not the bytes published at ${VERSION} -- corruption, a caching proxy, or tampering. Nothing was written to ${INSTALL_DIR}." >&2
	exit 1
fi
echo "install.sh: sha256 OK (${got})"

# ---------------------------------------------------------------------------
# Unpack + install a launcher (tan-cli#349). $tmp is now a verified .tar.gz of
# a --onedir freeze, not an executable -- extract it to a private staging dir
# first (unprivileged; the download+verify above never needed elevation
# either), THEN move the unpacked tree and write the launcher, mirroring the
# existing sudo-vs-not split below rather than growing a second one.
#
# The archive's one top-level entry is `tan/` (matching build_binary.sh's
# `shutil.make_archive(..., base_dir="tan")`), containing `tan` (the real
# executable) plus `_internal/` (its runtime). `mv` RENAMES that folder onto
# $LIB_DIR below rather than nesting it inside -- POSIX `mv src dst` makes
# `dst` BE `src` when `dst` does not already exist, it does not create
# `dst/src` -- so once moved the executable is at `$LIB_DIR/tan`, not
# `$LIB_DIR/tan/tan`. (Checked directly against a real archive while writing
# this: the nested path was the first thing tried, and it is wrong.)
# ---------------------------------------------------------------------------
tar -xzf "$tmp" -C "$stage"
if [ ! -x "$stage/tan/tan" ] && [ ! -f "$stage/tan/tan" ]; then
	echo "install.sh: ${asset} did not contain tan/tan after extraction -- archive layout changed?" >&2
	exit 1
fi
chmod +x "$stage/tan/tan"

dest="${INSTALL_DIR}/tan"
LIB_DIR="${INSTALL_DIR}/tan-cli-lib"
# A thin POSIX launcher, not a symlink: a symlink straight to $LIB_DIR/tan
# would still put a plain, unshimmed binary on PATH, which is fine for `tan`
# itself but gives a future reader nowhere obvious to add a wrapper concern
# (e.g. an env var) without editing the generated tree in place.
launcher="$(mktemp)"
cat >"$launcher" <<LAUNCHER
#!/bin/sh
# Generated by tan install.sh (tan-cli#349) -- do not edit by hand.
# Re-run install.sh to update both this launcher and $LIB_DIR.
exec "${LIB_DIR}/tan" "\$@"
LAUNCHER
chmod +x "$launcher"

# User-local dir: create + move without sudo. A non-writable dir (e.g. the
# --system /usr/local/bin) needs elevated permission -> use sudo explicitly so
# the admin step is visible, never silent. `rm -rf "$LIB_DIR"` first so a
# re-install replaces the old freeze wholesale rather than merging trees.
if mkdir -p "$INSTALL_DIR" 2>/dev/null && [ -w "$INSTALL_DIR" ]; then
	rm -rf "$LIB_DIR"
	mv "$stage/tan" "$LIB_DIR"
	mv "$launcher" "$dest"
else
	echo "install.sh: ${INSTALL_DIR} needs elevated permission -- running sudo (admin)."
	sudo mkdir -p "$INSTALL_DIR"
	sudo rm -rf "$LIB_DIR"
	sudo mv "$stage/tan" "$LIB_DIR"
	sudo mv "$launcher" "$dest"
	sudo chmod +x "$dest"
fi
# $tmp/$stage/$launcher have all been consumed by the moves above; $sums has
# not, so clear the trap only after removing it by hand -- otherwise a
# successful install is the one path that leaves a temp file behind.
rm -f "$sums" "$tmp"
rm -rf "$stage"
trap - EXIT

echo "install.sh: installed tan -> ${dest} (runtime: ${LIB_DIR})"
case ":${PATH}:" in
*":${INSTALL_DIR}:"*)
	: # already on PATH -- 'tan' works from any shell
	;;
*)
	if [ "$MODIFY_PATH" = "1" ]; then
		# Pick the login shell's rc file, append the PATH line (idempotent), and
		# announce it -- never edit a dotfile silently. This is what makes a
		# no-sudo user-local install usable globally (notably on macOS, where
		# ~/.local/bin is not on the default PATH).
		case "$(basename "${SHELL:-/bin/sh}")" in
		zsh) rc="$HOME/.zshrc" ;;
		bash)
			if [ "$(uname -s)" = "Darwin" ]; then rc="$HOME/.bash_profile"; else rc="$HOME/.bashrc"; fi
			;;
		*) rc="$HOME/.profile" ;;
		esac
		if [ -f "$rc" ] && grep -qF "$INSTALL_DIR" "$rc" 2>/dev/null; then
			echo "install.sh: ${INSTALL_DIR} already referenced in ${rc} -- not modified."
		else
			printf '\n%s\n' "export PATH=\"${INSTALL_DIR}:\$PATH\"  # added by tan install.sh" >>"$rc"
			echo "install.sh: added ${INSTALL_DIR} to PATH in ${rc}."
			echo "install.sh: open a NEW shell (or run:  . \"${rc}\") to use 'tan' anywhere. Undo: delete that line."
		fi
	else
		echo "install.sh: ${INSTALL_DIR} is not on PATH -- add:  export PATH=\"${INSTALL_DIR}:\$PATH\"  (or re-run without --no-modify-path)"
	fi
	;;
esac
# The sha256 check above proves the BYTES are the ones the release published;
# it says nothing about whether THIS host can execute them (e.g. a glibc floor
# the host's libc is below -- the -gnu asset's dynamic loader then fails with
# a message like `GLIBC_2.xx not found`, on stderr). Capture stdout+stderr
# rather than discarding it: that line is the single most useful diagnostic a
# user in this situation can be handed, and reporting exit 0 anyway is a false
# "installed" for a binary that cannot run. A verified-but-unrunnable binary is
# removed rather than left at $dest: it is the correct bytes for a host this
# is NOT, and leaving it on PATH turns every later `tan` invocation into this
# same opaque failure instead of a clear "not installed".
if verify_out="$("$dest" --version 2>&1)"; then
	echo "install.sh: verified: ${verify_out}"
else
	echo "install.sh: installed binary failed to run: ${verify_out}" >&2
	rm -f "$dest"
	rm -rf "$LIB_DIR"
	echo "install.sh: removed ${dest} and ${LIB_DIR} -- install failed. If the message above names a GLIBC symbol, this host's glibc is older than the release floor; install from a checkout instead: git clone https://github.com/${REPO} && pip install ./tan-cli/python" >&2
	exit 1
fi
