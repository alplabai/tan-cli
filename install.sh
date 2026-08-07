#!/usr/bin/env sh
# SPDX-License-Identifier: Apache-2.0
#
# tan installer for Linux + macOS. Downloads the prebuilt `tan` release
# archive for this platform from GitHub Releases, unpacks it, and installs a
# launcher. By DEFAULT it installs to a user-local dir (~/.local/bin) so NO
# sudo/admin is needed. Pass --system to install to /usr/local/bin (that path
# needs elevated permission -> uses sudo).
#
# The asset's SHAPE depends on which release you install, and this script reads
# that off the release rather than assuming it (tan-cli#356):
#
#   * From v0.5.0 (tan-cli#349) the asset is a PyInstaller --onedir freeze
#     archived as a .tar.gz, not a raw executable: --onefile re-extracted its
#     whole runtime into a fresh temp dir on EVERY invocation, which measured
#     13-19 s on macOS (unsigned re-extracted .dylibs get re-verified by the OS
#     on every load). $INSTALL_DIR/tan is then a thin launcher script, not the
#     binary itself -- the unpacked freeze lives in $INSTALL_DIR/tan-cli-lib/.
#   * Every tag published BEFORE v0.5.0 -- including v0.4.1, which is what
#     `latest` resolves to today, and the v0.5.0-rc4 pre-release -- publishes a
#     RAW executable under the same triple with no extension. That one is
#     installed as-is, no launcher and no tan-cli-lib/.
#
#   curl -fsSL https://raw.githubusercontent.com/alplabai/tan-cli/main/install.sh | sh
#   ./install.sh [--version vX.Y.Z] [--dir <path>] [--system]
set -eu

REPO="alplabai/tan-cli"
# Where release assets are fetched from. Overridable for an internal mirror
# that carries the same <tag>/<asset> layout (and it is how this installer's own
# tests serve a fixture release offline). `latest` is still resolved against
# github.com -- a mirror hosts bytes, it does not decide which tag is current.
BASE_URL="${TAN_INSTALL_BASE_URL:-https://github.com/${REPO}/releases/download}"
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
		# Fires before the tag is even resolved -- deliberately, even though
		# that makes this refusal a HOST property, not a per-tag capability,
		# while the raw/archive selection below IS genuinely per-tag
		# (tan-cli#356 adversarial review, item 4): the retired Rust tags
		# (v0.4.1 and older) really do publish a `-unknown-linux-musl` asset
		# this script could fetch and run, but this installer does not
		# resurrect that path for ANY --version, past or future. A musl
		# consumer piecing together "works for old tags, refuses for new
		# ones, and the boundary is which --version you happened to type" is
		# a worse promise than one clear, tag-independent refusal -- and see
		# the `Linux)` case above for why v0.5.0+ cannot ship musl again even
		# if a future tag wanted to (a PyInstaller freeze dynamic-links the
		# loader it was built against). Documented the same way in
		# docs/release-contract.md's musl section, so this decision is not
		# something only this comment knows about.
		echo "install.sh: this host's libc is musl (e.g. Alpine) -- refusing before even resolving --version. This installer does not support musl for ANY tag, including older ones (v0.4.1 and earlier) that genuinely still publish a -unknown-linux-musl asset: from v0.5.0 the binary is a PyInstaller freeze, which cannot produce the static musl artefact those older Rust releases did, and the only Linux asset going forward is -unknown-linux-gnu, which cannot exec on a musl host." >&2
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

# One HTTP download, curl or wget, quiet about nothing. Both branches keep the
# flags they had inline before (`--proto '=https' --tlsv1.2`, and no -q on
# wget) -- the transport's own error IS the primary diagnostic, so it is never
# swallowed in favour of a guess.
#
# `--proto` pins the transport to the scheme the URL already names, so a
# redirect cannot silently downgrade it. Derived from $BASE_URL rather than
# spelled '=https' inline only so an overridden TAN_INSTALL_BASE_URL (a mirror,
# or this installer's own tests) may be plain http; the default base IS https,
# so the default pin is exactly the '=https' it has always been.
case "$BASE_URL" in
https://*) proto="=https" ;;
*) proto="=http,https" ;;
esac
download() { # $1 = url, $2 = output path; returns non-zero on failure
	if command -v curl >/dev/null 2>&1; then
		curl -fSL --proto "$proto" --tlsv1.2 -o "$2" "$1"
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
sums_url="${BASE_URL}/${VERSION}/checksums.txt"

tmp="$(mktemp)"
sums="$(mktemp)"
stage="$(mktemp -d)"
trap 'rm -f "$tmp" "$sums"; rm -rf "$stage"' EXIT

# ---------------------------------------------------------------------------
# Verify what lands against the checksums.txt published in the SAME release.
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
# the downloaded asset is still in $tmp here and the trap removes it.
#
# checksums.txt is fetched FIRST, before the asset, because it is now also the
# asset MANIFEST (see the selection block below). Checking for a sha256 tool
# first in turn means a host that has none refuses before spending a ~20 MB
# download, instead of after -- and a refusal after a long download reads like
# a download failure.
# ---------------------------------------------------------------------------
if command -v sha256sum >/dev/null 2>&1; then
	sha256_of() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
	sha256_of() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
	# Outcome 4. Deliberately NOT a warn-and-continue, and deliberately no
	# --no-verify escape hatch: a flag that turns the check off is the hole
	# this closes, wearing a consent form. coreutils, busybox and macOS all
	# ship one of these two, so reaching here means a genuinely unusual host.
	echo "install.sh: cannot verify -- neither sha256sum nor shasum is on PATH." >&2
	echo "install.sh: refusing to install an unverified binary. Install coreutils (sha256sum) or perl/shasum, then re-run." >&2
	exit 1
fi

echo "install.sh: fetching ${VERSION} checksums.txt..."
if ! download "$sums_url" "$sums" 2>/dev/null; then
	# Outcome 1: the digests could not be fetched. Says nothing about the
	# release's contents -- which is exactly why it must not be worded like a
	# mismatch.
	echo "install.sh: could not fetch ${sums_url}" >&2
	echo "install.sh: refusing to install -- that file is both the list of assets ${VERSION} publishes and the only thing to verify a download against, so without it there is nothing to fetch and nothing to check. This is a fetch failure, NOT evidence anything is wrong with the release. Retry, or check a proxy/firewall." >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# WHICH SHAPE does this release publish? (tan-cli#356)
#
# From v0.5.0 an asset is a .tar.gz of a --onedir freeze (tan-cli#349). Every
# tag published before it -- v0.4.1, which is what `latest` resolves to today,
# and the v0.5.0-rc4 pre-release -- publishes a RAW executable under the same
# triple with no extension. Requesting the archive unconditionally 404s on
# every tag that exists right now, which is what #356 reported.
#
# Decided by asking the release ITSELF which name it carries, through the
# checksums.txt just fetched: that file lists every asset in the release, it
# comes from the tag already pinned above, and it is fetched unconditionally
# anyway because it is the integrity source. So this costs no extra request and
# introduces no second source of truth -- a release is the only authority on
# what that release contains, and a tag cut years from now needs no edit here.
#
# Two alternatives, both rejected:
#
#   * Comparing the resolved version against v0.5.0. That is exactly the
#     "second source of truth that drifts" #349 rejected on the extension side,
#     and in POSIX sh it also means hand-rolling SemVer pre-release ordering --
#     v0.5.0-rc4 must sort BELOW v0.5.0, which a naive string or field compare
#     gets backwards. A bug in that comparison picks the wrong shape silently.
#   * Sniffing the downloaded bytes' magic number (gzip 1f 8b vs an ELF/Mach-O
#     header), which IS #349's own rule for the extension. It does not transfer
#     here: the extension holds a file at a path it has already fetched, so its
#     bytes are in hand before the question is asked. These two shapes have
#     different NAMES, so a name must be chosen before there are any bytes to
#     sniff at all.
#
# None of this weakens the integrity check. The digest still comes from this
# same file, and it is still compared BEFORE the archive is unpacked or
# anything is written to $INSTALL_DIR.
# ---------------------------------------------------------------------------
digest_for() { awk -v a="$1" '$2 == a { print $1 }' "$sums" | head -1; }

# .tar.gz, never .zip: install.sh only ever targets Linux/macOS (Windows uses
# install.ps1, whose asset is the .zip build_binary.sh produces for that OS).
archive_asset="tan-${arch_part}-${os_part}.tar.gz"
raw_asset="tan-${arch_part}-${os_part}"
# Archive first, so a release carrying both is installed in the current shape
# rather than the legacy one.
asset="$archive_asset"
layout="archive"
want="$(digest_for "$asset")"
if [ -z "${want:-}" ]; then
	asset="$raw_asset"
	layout="raw"
	want="$(digest_for "$asset")"
fi
if [ -z "${want:-}" ]; then
	# Outcome 2, widened by #356: this release lists no asset for this platform
	# under EITHER name. Previously this could only be read as "the release
	# forgot to checksum an asset it shipped"; now it also covers "this
	# platform has no asset here at all", so it must name both possibilities
	# rather than assert the rarer one.
	echo "install.sh: ${VERSION} lists no asset for ${arch_part}-${os_part} in its checksums.txt -- neither ${archive_asset} nor ${raw_asset}." >&2
	case "${arch_part}-${os_part}" in
	aarch64-unknown-linux-gnu)
		echo "install.sh: note -- there is no prebuilt Linux arm64 asset from v0.5.0 onward. The binary is a frozen build that must be produced on the architecture it runs on, and the release builds no arm64 Linux. Install from a checkout instead: git clone https://github.com/${REPO} && pip install ./tan-cli/python" >&2
		;;
	esac
	echo "install.sh: refusing to install. Check what ${VERSION} publishes: https://github.com/${REPO}/releases -- if an asset for this platform IS listed there, the release is incomplete (shipped but left out of checksums.txt) and should be reported against ${REPO}; either way there is nothing here to verify against." >&2
	exit 1
fi

url="${BASE_URL}/${VERSION}/${asset}"
echo "install.sh: downloading ${asset} (${VERSION})..."
if ! download "$url" "$tmp"; then
	# The transport error above says THAT it failed, never why. Unlike before
	# #356 this is no longer the place a "never published" 404 surfaces -- the
	# selection above already proved the release lists this asset -- so a
	# failure here is a transport problem or a release whose checksums.txt and
	# uploaded assets disagree.
	echo "install.sh: download failed: ${url}" >&2
	echo "install.sh: refusing to install. ${VERSION}'s checksums.txt lists ${asset}, so the file is expected to exist -- this is most likely a network/proxy failure. If it is a 404, that release is inconsistent: https://github.com/${REPO}/releases" >&2
	exit 1
fi

got="$(sha256_of "$tmp")"
if [ "$got" != "$want" ]; then
	# Outcome 3: the one that means something is actually wrong.
	echo "install.sh: SHA256 MISMATCH for ${asset} (${VERSION})" >&2
	echo "install.sh:   expected ${want}" >&2
	echo "install.sh:   got      ${got}" >&2
	echo "install.sh: refusing to install. The downloaded bytes are not the bytes published at ${VERSION} -- corruption, a caching proxy, or tampering. Nothing was written to ${INSTALL_DIR}." >&2
	exit 1
fi
echo "install.sh: sha256 OK (${got})"

dest="${INSTALL_DIR}/tan"
LIB_DIR="${INSTALL_DIR}/tan-cli-lib"

# ---------------------------------------------------------------------------
# Prepare $payload -- the one file that ends up at $dest -- per layout. Both
# branches finish unprivileged, in temp space: the commit block below is the
# only thing that ever touches $INSTALL_DIR, so it is the only thing that ever
# needs sudo.
#
# archive (v0.5.0+, tan-cli#349): $tmp is a verified .tar.gz of a --onedir
# freeze, not an executable. The archive's one top-level entry is `tan/`
# (matching build_binary.sh's `shutil.make_archive(..., base_dir="tan")`),
# containing `tan` (the real executable) plus `_internal/` (its runtime). `mv`
# RENAMES that folder onto $LIB_DIR below rather than nesting it inside --
# POSIX `mv src dst` makes `dst` BE `src` when `dst` does not already exist, it
# does not create `dst/src` -- so once moved the executable is at
# `$LIB_DIR/tan`, not `$LIB_DIR/tan/tan`. (Checked directly against a real
# archive while writing this: the nested path was the first thing tried, and it
# is wrong.)
#
# raw (every tag before v0.5.0, tan-cli#356): $tmp already IS the executable.
# It becomes $dest directly and there is no $LIB_DIR at all.
# ---------------------------------------------------------------------------
if [ "$layout" = "archive" ]; then
	tar -xzf "$tmp" -C "$stage"
	if [ ! -f "$stage/tan/tan" ]; then
		echo "install.sh: ${asset} did not contain tan/tan after extraction -- archive layout changed?" >&2
		exit 1
	fi
	chmod +x "$stage/tan/tan"
	# a+rX over the whole tree, not just +x on the executable: `mktemp -d` makes
	# $stage 0700, so a --system install would otherwise leave a root-owned 0700
	# runtime under /usr/local/bin that the launcher on PATH cannot read for any
	# other user -- an install that only works for the account that ran sudo.
	chmod -R a+rX "$stage/tan"
	staged_bin="$stage/tan/tan"

	# A thin POSIX launcher, not a symlink: a symlink straight to $LIB_DIR/tan
	# would still put a plain, unshimmed binary on PATH, which is fine for `tan`
	# itself but gives a future reader nowhere obvious to add a wrapper concern
	# (e.g. an env var) without editing the generated tree in place.
	payload="$(mktemp)"
	cat >"$payload" <<LAUNCHER
#!/bin/sh
# Generated by tan install.sh (tan-cli#349) -- do not edit by hand.
# Re-run install.sh to update both this launcher and $LIB_DIR.
exec "${LIB_DIR}/tan" "\$@"
LAUNCHER
else
	chmod +x "$tmp"
	staged_bin="$tmp"
	payload="$tmp"
fi

# ---------------------------------------------------------------------------
# Health-check BEFORE anything is committed (tan-cli#434). The sha256 check
# above proves the downloaded BYTES are the ones the release published; it
# says nothing about whether THIS host can execute them. Two very different
# hosts fail here and must never read the same (tan-cli#490):
#
#   * A glibc floor the host's libc is below -- the -gnu asset's dynamic
#     loader runs and THEN fails, with a message like `GLIBC_2.xx not found`
#     on stderr; the shell's exit code for that is whatever the loader
#     returns, not 126.
#   * $TMPDIR (or the /tmp `mktemp` falls back to) is mounted `noexec` --
#     common on CIS/STIG-hardened images, which is exactly where customers
#     install from via `curl | sh`. exec(2) never gets to run the file at
#     all; every POSIX shell reports that as exit 126 with "...: Permission
#     denied" on stderr. Exit 126 ALONE is not enough to tell noexec apart
#     from a genuinely corrupt payload -- dash reports a mangled/non-ELF file
#     as exit 126 too, just with "Exec format error" instead (measured) -- so
#     both the code and the message text are checked below.
#
# On the noexec signature this retries ONCE, staged inside $INSTALL_DIR
# instead of $TMPDIR: $INSTALL_DIR has to be exec-able anyway for `tan` to
# ever run once installed, so if that mount refuses execution too there is
# nothing short of a different --dir that will help, and the final message
# says so instead of pointing at glibc. The retry only runs when
# $INSTALL_DIR is writable without sudo (the default `--dir "$HOME/.local/bin"`
# case this bug was filed against) -- a --system install prints the
# diagnosis and stops rather than auto-retrying as root, which would mean
# extracting/running an only-checksum-verified payload with root privilege.
# ---------------------------------------------------------------------------
run_health_check() { # $1 = path to the binary to run; sets $verify_out, $health_rc
	if verify_out="$("$1" --version 2>&1)"; then
		health_rc=0
	else
		health_rc=$?
	fi
}
is_noexec_signature() { # $1 = exit code, $2 = captured output
	[ "$1" = "126" ] || return 1
	case "$2" in
	*"Permission denied"*) return 0 ;;
	*) return 1 ;;
	esac
}

run_health_check "$staged_bin"

retried=0
if is_noexec_signature "$health_rc" "$verify_out" && mkdir -p "$INSTALL_DIR" 2>/dev/null && [ -w "$INSTALL_DIR" ]; then
	retried=1
	echo "install.sh: staged binary failed to execute (exit 126) -- that filesystem is very likely mounted noexec. Retrying staged inside ${INSTALL_DIR}..."
	retry_dir="$(mktemp -d "${INSTALL_DIR}/.tan-install-retry.XXXXXX" 2>/dev/null || true)"
	if [ -n "$retry_dir" ]; then
		old_stage="$stage"
		if [ "$layout" = "archive" ]; then
			if mv "$old_stage/tan" "$retry_dir/tan" 2>/dev/null; then
				chmod -R a+rX "$retry_dir/tan"
				rm -rf "$old_stage"
				stage="$retry_dir"
				staged_bin="$stage/tan/tan"
				run_health_check "$staged_bin"
			else
				rm -rf "$retry_dir"
			fi
		else
			if mv "$tmp" "$retry_dir/tan" 2>/dev/null; then
				chmod +x "$retry_dir/tan"
				rm -rf "$old_stage"
				stage="$retry_dir"
				staged_bin="$retry_dir/tan"
				payload="$staged_bin"
				run_health_check "$staged_bin"
			else
				rm -rf "$retry_dir"
			fi
		fi
	fi
fi

if [ "$health_rc" = "0" ]; then
	echo "install.sh: staged binary verified: ${verify_out}"
else
	echo "install.sh: newly downloaded binary failed to run: ${verify_out}" >&2
	if [ -e "$dest" ] || [ -e "$LIB_DIR" ]; then
		echo "install.sh: refusing to install -- your existing installation at ${dest} was never touched." >&2
	else
		echo "install.sh: refusing to install -- no previous installation existed, so there is nothing to fall back to." >&2
	fi
	echo "install.sh: PATH was not modified." >&2
	if is_noexec_signature "$health_rc" "$verify_out"; then
		if [ "$retried" = "1" ]; then
			echo "install.sh: exit 126 (Permission denied) persisted even after staging inside ${INSTALL_DIR} -- that filesystem is very likely ALSO mounted noexec." >&2
		else
			echo "install.sh: exit 126 (Permission denied) with the execute bit already set almost always means the staging filesystem is mounted noexec -- and ${INSTALL_DIR} was not writable without elevation, so no retry was attempted there." >&2
		fi
		echo "install.sh: this is a mount option on this host, NOT a broken download -- the sha256 check above already proved the downloaded bytes match the release. Retry with a TMPDIR (and/or --dir) that permits execution, e.g.:  TMPDIR=\"\$HOME/.cache\" sh install.sh ...   or   sh install.sh --dir <an exec-able path>" >&2
	else
		echo "install.sh: If the message above names a GLIBC symbol, this host's glibc is older than the release floor; install from a checkout instead: git clone https://github.com/${REPO} && pip install ./tan-cli/python" >&2
	fi
	exit 1
fi

# User-local dir: create + move without sudo. A non-writable dir (e.g. the
# --system /usr/local/bin) needs elevated permission -> use sudo explicitly so
# the admin step is visible, never silent. A function rather than a `$sudo`
# variable so every expansion below stays quoted (getting-started.yml runs
# `shellcheck --shell=sh` over this file).
if mkdir -p "$INSTALL_DIR" 2>/dev/null && [ -w "$INSTALL_DIR" ]; then
	as_root() { "$@"; }
else
	echo "install.sh: ${INSTALL_DIR} needs elevated permission -- running sudo (admin)."
	as_root() { sudo "$@"; }
	as_root mkdir -p "$INSTALL_DIR"
fi

# ---------------------------------------------------------------------------
# Commit: back up whatever is already at $dest/$LIB_DIR, swap the
# already-verified payload into place, and if any step of the swap fails, put
# the backup right back -- a failed upgrade must leave the user exactly where
# they started, never with neither binary (tan-cli#434). $dest.bak/$LIB_DIR.bak
# share a parent with $dest/$LIB_DIR, so the backup `mv` is a same-filesystem
# rename, not a copy.
# ---------------------------------------------------------------------------
had_dest_backup=0
had_lib_backup=0
if [ -e "$dest" ]; then
	as_root rm -rf "${dest}.bak"
	as_root mv "$dest" "${dest}.bak"
	had_dest_backup=1
fi
if [ -e "$LIB_DIR" ]; then
	as_root rm -rf "${LIB_DIR}.bak"
	as_root mv "$LIB_DIR" "${LIB_DIR}.bak"
	had_lib_backup=1
fi

restore_previous() { # sets $restore_ok; never lets its own exit status kill the script under set -e
	restore_ok=1
	as_root rm -rf "$dest" "$LIB_DIR"
	if [ "$had_dest_backup" = "1" ]; then
		as_root mv "${dest}.bak" "$dest" || restore_ok=0
	fi
	if [ "$had_lib_backup" = "1" ]; then
		as_root mv "${LIB_DIR}.bak" "$LIB_DIR" || restore_ok=0
	fi
	return 0
}

# Wholesale, not a merge, so a re-install replaces the old freeze rather than
# mixing two releases' files (an archive install over a previous raw install,
# or vice versa, must not leave the other layout's files behind either).
commit_failed=0
if [ "$layout" = "archive" ]; then
	as_root mv "$stage/tan" "$LIB_DIR" || commit_failed=1
fi
if [ "$commit_failed" = "0" ]; then
	as_root mv "$payload" "$dest" || commit_failed=1
fi
if [ "$commit_failed" = "1" ]; then
	echo "install.sh: failed to place the new install under ${INSTALL_DIR} -- rolling back." >&2
	restore_previous
	if [ "$had_dest_backup" = "0" ] && [ "$had_lib_backup" = "0" ]; then
		echo "install.sh: no previous installation existed; nothing to restore. Install failed." >&2
	elif [ "$restore_ok" = "1" ]; then
		echo "install.sh: previous installation restored -- 'tan' still works as before. Install failed." >&2
	else
		echo "install.sh: WARNING -- could not fully restore the previous installation. A backup remains at ${dest}.bak (and ${LIB_DIR}.bak, if that existed) -- move it back by hand: mv \"${dest}.bak\" \"${dest}\"" >&2
	fi
	echo "install.sh: PATH was not modified." >&2
	exit 1
fi
# 755, not `chmod +x`: both $payload sources are `mktemp` files, i.e. 0600, and
# +x on 0600 is 0700 -- an owner-only binary, which is precisely what --system
# into /usr/local/bin must not produce.
as_root chmod 755 "$dest"
# Commit succeeded -- the backup is no longer needed. rm -rf on an absent path
# is a no-op, so this needs no had_*_backup guard.
as_root rm -rf "${dest}.bak" "${LIB_DIR}.bak"

# $payload and (on the archive path) $stage have been consumed by the moves
# above; $sums and, on the archive path, $tmp have not, so clear the trap only
# after removing them by hand -- otherwise a successful install is the one path
# that leaves a temp file behind.
rm -f "$sums" "$tmp"
rm -rf "$stage"
trap - EXIT

if [ "$layout" = "archive" ]; then
	echo "install.sh: installed tan -> ${dest} (runtime: ${LIB_DIR})"
else
	echo "install.sh: installed tan -> ${dest}"
fi
case ":${PATH}:" in
*":${INSTALL_DIR}:"*)
	: # already on PATH -- 'tan' works from any shell
	;;
*)
	if [ "$MODIFY_PATH" = "1" ]; then
		# Pick the login shell's rc file, append the PATH line (idempotent), and
		# announce it -- never edit a dotfile silently. This is what makes a
		# no-sudo user-local install usable globally (notably on macOS, where
		# ~/.local/bin is not on the default PATH). Reached only after the commit
		# above succeeded, so a failed install never leaves this line behind
		# (tan-cli#434).
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
