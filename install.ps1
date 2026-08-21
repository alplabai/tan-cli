# SPDX-License-Identifier: Apache-2.0
#
# tan installer for Windows. Downloads the prebuilt tan release archive for
# this platform from GitHub Releases, expands it, and installs a launcher. By
# DEFAULT it installs under %LOCALAPPDATA%\Programs\tan and updates the USER
# Path, so NO admin is needed. Pass -System to install under %ProgramFiles%
# and update the MACHINE Path (that requires an elevated / "Run as
# administrator" PowerShell).
#
# The asset's SHAPE depends on which release you install, and this script reads
# that off the release rather than assuming it (tan-cli#356):
#
#   * From v0.5.0 (tan-cli#349) the asset is a PyInstaller --onedir freeze
#     archived as a .zip, not a raw tan.exe: --onefile re-extracted its whole
#     runtime into a fresh temp dir on EVERY invocation, which measured 13-19 s
#     on macOS (unsigned re-extracted .dylibs get re-verified by the OS on every
#     load) and even on Windows measured >2x slower per-invocation than
#     --onedir. $Dir\tan.cmd is then a thin launcher, not the executable itself
#     -- the unpacked freeze lives in $Dir\tan-cli-lib\.
#   * Every tag published BEFORE v0.5.0 -- v0.4.1 and the v0.5.0-rc4
#     pre-release -- publishes a raw tan.exe. That one is installed as
#     $Dir\tan.exe, no launcher and no tan-cli-lib\. `latest` resolves to v0.5.1
#     today, so the no-argument install takes the archive path; the raw path is
#     reached only via -Version on an older tag.
#
# Mirrors install.sh's shape for the .tar.gz side of the same change.
#
#   irm https://raw.githubusercontent.com/alplabai/tan-cli/main/install.ps1 | iex
#   .\install.ps1 [-Version vX.Y.Z] [-Dir <path>] [-System] [-NoModifyPath]
[CmdletBinding()]
param(
	[string]$Version = "latest",
	[string]$Dir = "",
	[switch]$System,
	# Skip the Path update. install.sh has had --no-modify-path since it started
	# editing rc files; this script writes to the USER (or MACHINE) environment
	# in the registry, which is the more persistent of the two, and had no way
	# to opt out at all. Also what lets this script's own tests run without
	# leaving a pile of dead temp directories on the developer's Path.
	[switch]$NoModifyPath
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repo = "alplabai/tan-cli"
# Where release assets are fetched from. Overridable for an internal mirror that
# carries the same <tag>/<asset> layout (and it is how this installer's own tests
# serve a fixture release offline). `latest` is still resolved against GitHub's
# API -- a mirror hosts bytes, it does not decide which tag is current.
$baseUrl = if ($env:TAN_INSTALL_BASE_URL) { $env:TAN_INSTALL_BASE_URL } else { "https://github.com/$repo/releases/download" }

# host arch -> rust target arch part
$archRaw = $env:PROCESSOR_ARCHITECTURE
if ($env:PROCESSOR_ARCHITEW6432) { $archRaw = $env:PROCESSOR_ARCHITEW6432 }
switch ($archRaw) {
	"AMD64" { $archPart = "x86_64" }
	"ARM64" { $archPart = "aarch64" }
	default { throw "install.ps1: unsupported architecture '$archRaw'" }
}
$archiveAsset = "tan-$archPart-pc-windows-msvc.zip"
$rawAsset = "tan-$archPart-pc-windows-msvc.exe"

# install dir + PATH scope: user-local (no admin) by default, machine with -System (admin)
if ($System) {
	$scope = "Machine"
	if (-not $Dir) { $Dir = Join-Path $env:ProgramFiles "tan" }
} else {
	$scope = "User"
	if (-not $Dir) { $Dir = Join-Path $env:LOCALAPPDATA "Programs\tan" }
}
# tan-cli#490: normalise $Dir to an absolute path before it is used anywhere.
# The generated launcher is already immune to a relative -Dir (it resolves
# itself through `%~dp0`, its own directory, never a baked-in path) -- but the
# Path update below is not: a relative -Dir would be written into the
# User/Machine registry Path verbatim, permanently putting a CWD-relative
# entry on PATH.
#
# [System.IO.Path]::GetFullPath resolves a relative path against
# [Environment]::CurrentDirectory, NOT against the shell's own working
# directory -- and NEITHER Windows PowerShell 5.1 NOR PowerShell 7+ updates
# that .NET-process-global property on `Set-Location`/`cd`, only $PWD. This is
# not a 5.1-only quirk that 7 fixed: $PWD is PowerShell's own provider-scoped
# location (it has to be, since a provider path like `HKCU:\` or `Cert:\` has
# no meaning as a filesystem CurrentDirectory at all), and that stays
# decoupled from .NET's CurrentDirectory on every version -- measured directly
# (PowerShell 7.4.6: `Set-Location` into a subdirectory, then compare
# `$PWD.Path` against `[Environment]::CurrentDirectory`) confirms the two
# diverge on 7 exactly as they do on 5.1. A user who does `cd .\tools;
# irm .../install.ps1 | iex -Dir .\bin` would have `.\bin` resolved against
# wherever the PROCESS started (often $HOME or C:\WINDOWS\system32, not
# .\tools), silently installing to a directory the user never asked for, on
# EITHER PowerShell version -- which is exactly the case that must not
# regress. [System.IO.Path]::Combine passes an already-absolute $Dir through
# unchanged (it returns the second argument verbatim when it is rooted) and
# anchors a relative one against $PWD.Path, which both 5.1 and 7 keep
# accurate.
$Dir = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($PWD.Path, $Dir))

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# `latest` is a REDIRECT, and resolving it twice is not the same as resolving it
# once: a release cut between the binary fetch and the checksums fetch would
# have us verify one release's asset against another release's digests. Rare,
# silent, and it yields a WRONG VERDICT rather than an error -- so pin the tag up
# front and build both URLs from it.
#
# The digest for a given filename really does move between tags: at v0.4.0-rc1
# tan-x86_64-pc-windows-msvc.exe was f159c1dc..., at v0.4.0 it was a80fb5da...,
# same asset name (pre-v0.5.0-rc4, when the asset was a raw .exe rather than
# today's .zip -- the property holds identically for the archive). Anything
# that caches or hardcodes a digest is wrong by construction.
# Resolved through the API's `tag_name` rather than by inspecting the
# /releases/latest redirect, which install.sh uses. Not gratuitous divergence --
# each host gets the mechanism that is actually robust on it:
#
#   * POSIX sh has curl's `-w '%{url_effective}'`, which reports the final URL
#     in one flag and no JSON parser (jq is not guaranteed on a fresh host).
#   * PowerShell has no stable equivalent. Reading a 3xx's Location differs
#     between 5.1 and 7, and under this script's own `Set-StrictMode -Version
#     Latest` a missing `$_.Exception.Response` property THROWS rather than
#     yielding $null -- so the defensive form is the one that breaks. The API's
#     `tag_name` is one documented field that behaves identically on both.
#
# Both endpoints exclude prereleases the same way, so the two scripts resolve
# `latest` to the same tag. That matters right now: v0.4.0 is marked prerelease,
# so `latest` is NOT the highest version number.
if ($Version -eq "latest") {
	Write-Host "install.ps1: resolving the latest release tag..."
	$tag = $null
	try {
		$rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" `
			-UseBasicParsing -Headers @{ "User-Agent" = "tan-install.ps1" }
		$tag = $rel.tag_name
	} catch {
		# Swallowed on purpose: every failure here lands on the same refusal
		# below. An unauthenticated API rate limit (60/hr per IP) is the
		# realistic one, and it must not be able to produce a WRONG tag --
		# only no tag.
		$tag = $null
	}
	if ($tag) {
		$Version = $tag
		Write-Host "install.ps1: latest is $Version."
	} else {
		Write-Error "install.ps1: could not resolve which release 'latest' points at. Refusing to install -- without a tag there is no checksums.txt to verify against. Retry, or pass an explicit -Version vX.Y.Z."
		exit 1
	}
}
$sumsUrl = "$baseUrl/$Version/checksums.txt"

# tan-cli#803: `$Dir` itself is NOT created here any more. It used to be
# created right at this point, before checksums.txt was even fetched -- so
# EVERY refusal below (fetch failure, no asset for this platform, download
# failure, sha256 mismatch, bad archive layout, the health check) left $Dir
# (and every parent it created) orphaned on disk, even though nothing had
# been downloaded yet. install.sh already gets this right -- its own
# "`mkdir -p "$INSTALL_DIR"` still runs later, but only once an install is
# actually going ahead" comment -- and this script needs the same ordering:
# `New-Item -ItemType Directory -Force -Path $Dir` (the ps1 equivalent of
# `mkdir -p`) now runs only once the health check below has PASSED, right
# before the commit section.
#
# $dirFirstNewAncestor is still recorded here, unconditionally and up front,
# because it has to reflect what "new" means relative to the state BEFORE
# this run touched anything -- computing it later, after the noexec-retry
# path below may have already created part of the chain, would see its own
# just-created directories as "already there" and refuse to clean them up.
# It is only ACTED on, though, if this run is the one that actually created
# something under it: see $dirCreatedForRetry below, and the commit-block
# comment further down for install.sh's `install_dir_created_for_retry`
# ordering this now mirrors.
$dirFirstNewAncestor = $null
if (-not (Test-Path -LiteralPath $Dir)) {
	$ancestor = $Dir
	while (-not (Test-Path -LiteralPath $ancestor)) {
		$dirFirstNewAncestor = $ancestor
		$parent = Split-Path -Path $ancestor -Parent
		if (-not $parent -or $parent -eq $ancestor) { break }
		$ancestor = $parent
	}
}
# Set once, only if the noexec-retry block below ends up creating $Dir itself
# (see there) -- gates the health-check-failure cleanup further down so it
# never removes a $Dir a PREVIOUS install already left in place.
$dirCreatedForRetry = $false
$LibDir = Join-Path $Dir "tan-cli-lib"

# Download to TEMP, never straight into $Dir. Writing into the destination
# first and checking afterwards means a mismatched file has already landed --
# and on Windows it may already be locked, or already on PATH, by the time the
# check fails. Verify, then unpack/move.
#
# One GUID stem for all three temp paths, so `finally` can clear them with a
# single wildcard even though $tmp's extension is not known until the asset has
# been chosen (Expand-Archive REFUSES a path that does not end in .zip, so the
# extension cannot just be left off).
$tmpBase = Join-Path ([IO.Path]::GetTempPath()) ("tan-" + [Guid]::NewGuid().ToString("N"))
$sumsTmp = "$tmpBase.checksums.txt"
$stage = "$tmpBase.stage"
try {
	# -----------------------------------------------------------------------
	# Verify what lands against the checksums.txt published in the SAME release.
	#
	# TLS says we talked to github.com. It does not say github.com handed us the
	# bytes we published, and it says nothing about a proxy, a cache, or a
	# truncated write. checksums.txt already exists at every tag and now covers
	# the ARCHIVES rather than raw binaries, and alp-sdk-vscode already verifies
	# its own managed download against it (alplabai/alp-sdk-vscode#389) and
	# refuses a mismatch. Until this landed the two acquisition paths for the
	# same binary disagreed about whether they check it -- and the unverified
	# one is what the extension's "Install tan CLI (global)" button runs, whose
	# result the extension's resolver then PREFERS over its own verified copy,
	# on every activation, indefinitely.
	#
	# THREE distinct outcomes, three distinct messages, all refusing. Being
	# offline behind a corporate proxy and being handed a tampered archive are
	# not the same situation and must not read the same. (Get-FileHash is built
	# in since PowerShell 4, so the POSIX script's fourth outcome -- no sha256
	# tool on PATH -- cannot arise here.) Nothing reaches $Dir on any of them.
	#
	# checksums.txt is fetched FIRST, before the asset, because it is now also
	# the asset MANIFEST -- see the selection block below.
	# -----------------------------------------------------------------------
	Write-Host "install.ps1: fetching $Version checksums.txt..."
	try {
		Invoke-WebRequest -Uri $sumsUrl -OutFile $sumsTmp -UseBasicParsing
	} catch {
		# Outcome 1: the digests could not be fetched. Says nothing about the
		# release's contents -- which is why it must not be worded like a
		# mismatch.
		Write-Error "install.ps1: could not fetch $sumsUrl`nRefusing to install -- that file is both the list of assets $Version publishes and the only thing to verify a download against, so without it there is nothing to fetch and nothing to check. This is a fetch failure, NOT evidence anything is wrong with the release. Retry, or check a proxy/firewall."
		exit 1
	}

	# -----------------------------------------------------------------------
	# WHICH SHAPE does this release publish? (tan-cli#356)
	#
	# From v0.5.0 the asset is a .zip of a --onedir freeze (tan-cli#349). Every
	# tag published before it -- v0.4.1 and the v0.5.0-rc4 pre-release --
	# publishes a raw tan.exe under the same triple. `latest` resolves to v0.5.1
	# today, so both shapes are installable and neither name can be assumed:
	# requesting the .zip unconditionally 404'd on every tag that existed when
	# #356 was reported.
	#
	# Decided by asking the release ITSELF which name it carries, through the
	# checksums.txt just fetched: that file lists every asset in the release, it
	# comes from the tag already pinned above, and it is fetched unconditionally
	# anyway because it is the integrity source. No extra request, and no second
	# source of truth -- a release is the only authority on what it contains.
	#
	# Two alternatives, both rejected (install.sh rejects them for the same
	# reasons; the two scripts must not disagree about which asset a tag has):
	#
	#   * Comparing $Version against v0.5.0. That is exactly the "second source
	#     of truth that drifts" #349 rejected on the extension side, and it also
	#     needs SemVer pre-release ordering -- v0.5.0-rc4 sorts BELOW v0.5.0,
	#     which [version] does not model at all ([version]"0.5.0-rc4" does not
	#     even parse). A bug there picks the wrong shape silently.
	#   * Sniffing the downloaded bytes' magic number (PK.. vs MZ), which IS
	#     #349's own rule for the extension. It does not transfer here: the
	#     extension holds a file at a path it has already fetched, so its bytes
	#     are in hand before the question is asked. These two shapes have
	#     different NAMES, so a name must be chosen before there are any bytes.
	#
	# None of this weakens the integrity check: the digest still comes from this
	# same file and is still compared BEFORE anything is expanded or written to
	# $Dir.
	# -----------------------------------------------------------------------
	$sumsLines = Get-Content -LiteralPath $sumsTmp
	function Get-DigestFor([string]$name) {
		foreach ($line in $sumsLines) {
			$parts = $line -split '\s+', 2
			# Exact field match, never a substring: `tan-x86_64-pc-windows-msvc.exe`
			# is a SUFFIX of nothing here, but `tan-x86_64-pc-windows-msvc` is a
			# prefix of both names this script asks about.
			if ($parts.Count -eq 2 -and $parts[1].Trim() -eq $name) { return $parts[0].Trim() }
		}
		return $null
	}

	# Archive first, so a release carrying both is installed in the current
	# shape rather than the legacy one.
	$asset = $archiveAsset
	$layout = "archive"
	$want = Get-DigestFor $asset
	if (-not $want) {
		$asset = $rawAsset
		$layout = "raw"
		$want = Get-DigestFor $asset
	}
	if (-not $want) {
		# Outcome 2, widened by #356: this release lists no asset for this
		# platform under EITHER name. It used to mean only "the release forgot
		# to checksum an asset it shipped"; it now also covers "this platform
		# has no asset here at all", so it must name both rather than assert the
		# rarer one.
		Write-Host "install.ps1: $Version lists no asset for $archPart-pc-windows-msvc in its checksums.txt -- neither $archiveAsset nor $rawAsset." -ForegroundColor Red
		if ($archPart -eq "aarch64") {
			Write-Error "install.ps1: there is no prebuilt Windows arm64 asset from v0.5.0 onward. The binary is a frozen build that must be produced on the architecture it runs on, and the release builds no Windows arm64 leg. Install from a checkout instead: git clone https://github.com/$repo && pip install ./tan-cli/python"
		} else {
			Write-Error "install.ps1: refusing to install. Check what $Version publishes: https://github.com/$repo/releases -- if an asset for this platform IS listed there, the release is incomplete (shipped but left out of checksums.txt) and should be reported against $repo; either way there is nothing here to verify against."
		}
		exit 1
	}

	$tmp = if ($layout -eq "archive") { "$tmpBase.zip" } else { "$tmpBase.exe" }
	$url = "$baseUrl/$Version/$asset"
	Write-Host "install.ps1: downloading $asset ($Version)..."
	try {
		Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
	} catch {
		# Unlike before #356 this is no longer where a "never published" 404
		# surfaces -- the selection above already proved the release lists this
		# asset -- so a failure here is a transport problem, or a release whose
		# checksums.txt and uploaded assets disagree.
		Write-Host "install.ps1: download failed: $url" -ForegroundColor Red
		Write-Error "install.ps1: refusing to install. $Version's checksums.txt lists $asset, so the file is expected to exist -- this is most likely a network/proxy failure. If it is a 404, that release is inconsistent: https://github.com/$repo/releases"
		exit 1
	}

	$got = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLower()
	if ($got -ne $want.ToLower()) {
		# Outcome 3: the one that means something is actually wrong.
		Write-Error "install.ps1: SHA256 MISMATCH for $asset ($Version)`n  expected $want`n  got      $got`nRefusing to install. The downloaded bytes are not the bytes published at $Version -- corruption, a caching proxy, or tampering. Nothing was written to $Dir."
		exit 1
	}
	Write-Host "install.ps1: sha256 OK ($got)"

	# -------------------------------------------------------------------------
	# Stage the payload on disk, unprivileged, without touching $Dir at all.
	#
	# archive (v0.5.0+, tan-cli#349): $tmp is a verified .zip of a --onedir
	# freeze, not an executable -- expand it into a private staging dir. The
	# archive's one top-level entry is `tan\`, matching build_binary.sh's
	# `shutil.make_archive(..., base_dir="tan")`, containing `tan.exe` (the real
	# executable) plus `_internal\` (its runtime).
	#
	# raw (every tag before v0.5.0): $tmp already IS tan.exe.
	# -------------------------------------------------------------------------
	$destCmd = Join-Path $Dir "tan.cmd"
	$destExe = Join-Path $Dir "tan.exe"
	$dest = if ($layout -eq "archive") { $destCmd } else { $destExe }

	if ($layout -eq "archive") {
		Expand-Archive -LiteralPath $tmp -DestinationPath $stage -Force
		$stagedExe = Join-Path $stage "tan\tan.exe"
		if (-not (Test-Path -LiteralPath $stagedExe)) {
			Write-Error "install.ps1: $asset did not contain tan\tan.exe after extraction -- archive layout changed?"
			exit 1
		}
	} else {
		$stagedExe = $tmp
	}

	# -------------------------------------------------------------------------
	# Health-check BEFORE anything under $Dir is touched (tan-cli#434). The
	# sha256 check above proves the downloaded BYTES are the ones the release
	# published; it says nothing about whether THIS host can execute them (e.g.
	# a missing runtime dependency, or security software that altered the
	# file). `& $stagedExe --version` with its exit code unchecked would not
	# fail the script even when the binary cannot run -- PowerShell does not
	# turn a non-zero native exit code into a terminating error on its own,
	# $ErrorActionPreference or not -- so the output is captured and
	# $LASTEXITCODE is checked explicitly. Running this against the STAGED copy,
	# before $Dir is touched, means a verified-but-unrunnable release never
	# costs the user their previous working install.
	#
	# tan-cli#490: on a host where a software-restriction / AppLocker policy
	# blocks execution from %TEMP% -- exactly the enterprise-hardened image
	# class customers install `tan` onto -- CreateProcess itself refuses and
	# .NET surfaces that as a Win32Exception "Access is denied", not as
	# anything that looks like a broken binary. That has to read differently
	# from an actual corrupt payload (a missing-dependency error, or "is not a
	# valid Win32 application"), so on that signature this retries the health
	# check against a COPY staged inside $Dir instead of %TEMP% -- $Dir already
	# has to permit running `tan` for the install to be usable at all -- before
	# settling on the generic "this may be a broken binary" wording. The copy
	# is discarded either way; $stage/$tmp (and the eventual commit) are
	# untouched by this, so a policy-driven retry failure still leaves nothing
	# under $Dir.
	#
	# tan-cli#490 review: gating on the "Access is denied" MESSAGE alone
	# (unlike install.sh's `is_noexec_signature`, which requires exit code
	# 126 AND the message text -- text alone is not enough there because a
	# genuinely corrupt/wrong-arch binary can print similar wording under a
	# different code) risked the same misattribution on Windows. There is no
	# meaningful process exit code to fall back on here -- CreateProcess never
	# started a process at all -- so the discriminator instead is the actual
	# Win32 status CreateProcess returned, read off the Win32Exception
	# PowerShell wraps the failure in, not a substring of its formatted
	# .Message.
	#
	# tan-cli#490 review, round 5: ERROR_ACCESS_DENIED (5) alone missed the
	# exact scenario the issue names -- the stock AppLocker/Software
	# Restriction Policy "block executables from %TEMP%" rule. That policy
	# class fails CreateProcess with ERROR_ACCESS_DISABLED_BY_POLICY (1260,
	# "This program is blocked by group policy"), which is not 5. The new
	# test at the time only reproduced an NTFS deny-execute ACE
	# (`icacls /deny`), which genuinely does yield 5, so nothing exercised
	# this path against the mechanism the issue actually names.
	#
	# tan-cli#490 review, round 5 also paired a second, real symbol,
	# ERROR_ACCESS_DISABLED_NO_SAFER_UI_BY_POLICY -- the same SAFER/SRP policy
	# class configured with NO user-facing notification -- with the wrong
	# number, 1261. 1261 is ERROR_REG_NAT_CONSUMPTION, an unrelated Itanium
	# invalid-register-value fault; round 6 caught that mismatch but, having
	# only checked the 1000-1299 and 1300-1699 system-error-codes pages,
	# concluded no such symbol exists at all and deleted it. It exists: it is
	# 786 (0x312), on the 500-999 page, confirmed against two Microsoft
	# sources -- learn.microsoft.com/windows/win32/debug/system-error-codes--500-999-
	# ("Access to %1 has been restricted by your Administrator by policy rule
	# %2.") and MS-ERREF (openspecs/windows_protocols/ms-erref, 0x00000312).
	# Both matched codes below (1260 and 786) are the same class of refusal
	# (CreateProcess never started the process at all) and get the same
	# "security policy" wording further down.
	# -------------------------------------------------------------------------
	function Get-Win32ErrorCode($exception) {
		$e = $exception
		while ($e) {
			if ($e -is [System.ComponentModel.Win32Exception]) { return $e.NativeErrorCode }
			$e = $e.InnerException
		}
		return $null
	}
	function Test-AccessDeniedSignature($win32ErrorCode) {
		if ($null -eq $win32ErrorCode) { return $false }
		return ($win32ErrorCode -eq 5) -or       # ERROR_ACCESS_DENIED
			($win32ErrorCode -eq 1260) -or       # ERROR_ACCESS_DISABLED_BY_POLICY
			($win32ErrorCode -eq 786)            # ERROR_ACCESS_DISABLED_NO_SAFER_UI_BY_POLICY
	}
	function Invoke-HealthCheck([string]$exePath) {
		try {
			$out = (& $exePath --version 2>&1 | Out-String).Trim()
			return @{ Out = $out; Exit = $LASTEXITCODE; Win32ErrorCode = $null }
		} catch {
			return @{ Out = $_.Exception.Message; Exit = 1; Win32ErrorCode = (Get-Win32ErrorCode $_.Exception) }
		}
	}

	$check = Invoke-HealthCheck $stagedExe
	$verifyOut = $check.Out
	$verifyExit = $check.Exit
	$verifyWin32Code = $check.Win32ErrorCode

	$retried = $false
	$retryStageErr = $null
	if ($verifyExit -ne 0 -and (Test-AccessDeniedSignature $verifyWin32Code)) {
		# tan-cli#803: $Dir's real creation is now deferred to the commit
		# section below (see the comment near $dirFirstNewAncestor), so unlike
		# before, $Dir usually does NOT exist yet at this point. The retry
		# needs a real directory under $Dir to stage a copy into -- create
		# $Dir itself first, but ONLY when it does not already exist, and
		# remember that THIS run is the one that created it. That mirrors
		# install.sh's own `install_dir_created_for_retry` (tan-cli#490 round
		# 9): if the health check still fails after retrying, the cleanup
		# below must undo only a $Dir this run created, never one a previous
		# install already left in place.
		if (-not (Test-Path -LiteralPath $Dir)) {
			try {
				New-Item -ItemType Directory -Force -Path $Dir -ErrorAction Stop | Out-Null
				$dirCreatedForRetry = $true
			} catch {
				$retryStageErr = $_.Exception.Message
			}
		}
		if (Test-Path -LiteralPath $Dir) {
			$retryDir = Join-Path $Dir (".tan-install-retry." + [Guid]::NewGuid().ToString("N"))
			try {
				New-Item -ItemType Directory -Path $retryDir | Out-Null
				$retryExe = Join-Path $retryDir (Split-Path -Leaf $stagedExe)
				Copy-Item -LiteralPath $stagedExe -Destination $retryExe -Force
				if ($layout -eq "archive") {
					# The onedir freeze needs its whole runtime tree beside the exe
					# to even start, not just the exe file by itself.
					Copy-Item -LiteralPath (Join-Path $stage "tan\_internal") -Destination (Join-Path $retryDir "_internal") -Recurse -Force
				}
				Write-Host "install.ps1: staged binary would not execute (Access is denied) -- a security policy on this host likely blocks running from its staging location. Retrying staged inside $Dir..."
				$retried = $true
				$check = Invoke-HealthCheck $retryExe
				$verifyOut = $check.Out
				$verifyExit = $check.Exit
				$verifyWin32Code = $check.Win32ErrorCode
			} catch {
				# $Dir could not be used for the retry -- capture the REAL reason
				# (mirrors install.sh's retry_stage_err, tan-cli#490: discarding
				# it is exactly the misattributed-diagnostic class #490 is
				# about) rather than guessing a cause the code never measured;
				# fall through to the failure branch below with the ORIGINAL
				# %TEMP% result.
				$retryStageErr = $_.Exception.Message
			} finally {
				Remove-Item -LiteralPath $retryDir -Recurse -Force -ErrorAction SilentlyContinue
			}
		}
	}

	if ($verifyExit -eq 0) {
		Write-Host "install.ps1: staged binary verified: $verifyOut"
	} else {
		Write-Host "install.ps1: newly downloaded binary failed to run: $verifyOut" -ForegroundColor Red
		if ((Test-Path -LiteralPath $destCmd) -or (Test-Path -LiteralPath $destExe) -or (Test-Path -LiteralPath $LibDir)) {
			Write-Host "install.ps1: your existing installation in $Dir was never touched."
		} else {
			Write-Host "install.ps1: no previous installation existed, so there is nothing to fall back to."
		}
		# tan-cli#490 round 9 / tan-cli#803: a refused install must not leave an
		# orphaned $Dir (or its freshly-created parents) behind. Since $Dir's
		# real creation is now deferred past this whole health-check phase
		# (see the comment near $dirFirstNewAncestor above), the ONLY way this
		# run could have created anything under $Dir before reaching here is
		# the noexec-retry block just above -- gated on $dirCreatedForRetry so
		# this never removes a $Dir a PREVIOUS install already left in place
		# (which $dirFirstNewAncestor alone would not distinguish: it is
		# computed once, up front, before the retry ever runs).
		#
		# tan-cli#490 review, round 10: `-Recurse` here was an UNGUARDED
		# recursive delete of a path that, with the default $Dir
		# (%LOCALAPPDATA%\Programs\tan), is %LOCALAPPDATA%\Programs itself --
		# a shared per-user root other installers populate. The whole
		# checksums fetch, asset download, sha256 check, and extraction all
		# fall inside this window (seconds to minutes on a real ~40 MB
		# asset), so anything another process wrote under that shared root
		# meanwhile was destroyed along with the orphan this run created.
		# Walk leaf ($Dir) up to the recorded first-new-ancestor, deleting
		# each directory NON-recursively: [System.IO.Directory]::Delete with
		# recursive=$false throws the instant a directory holds anything
		# this run did not put there (another process's write, or content
		# that already existed), which is exactly the wanted semantics --
		# remove only what this run created, and stop the moment something
		# else is in the way.
		if ($dirCreatedForRetry -and $dirFirstNewAncestor) {
			$dirWalkRemove = $Dir
			while ($true) {
				try { [System.IO.Directory]::Delete($dirWalkRemove, $false) } catch { }
				if ($dirWalkRemove -eq $dirFirstNewAncestor) { break }
				$dirWalkRemove = Split-Path -Path $dirWalkRemove -Parent
			}
		}
		if (Test-AccessDeniedSignature $verifyWin32Code) {
			if ($retried) {
				Write-Host "install.ps1: Access is denied persisted even after staging inside $Dir -- a security policy (AppLocker / Software Restriction Policy) very likely blocks running tan from there too, not only from %TEMP%." -ForegroundColor Red
			} elseif ($retryStageErr) {
				Write-Host "install.ps1: Access is denied, with the file freshly downloaded and sha256-verified, almost always means a security policy (AppLocker / Software Restriction Policy) blocks running an executable from %TEMP% -- not a broken download. (Could not stage a retry inside ${Dir}: $retryStageErr)" -ForegroundColor Red
			} else {
				Write-Host "install.ps1: Access is denied, with the file freshly downloaded and sha256-verified, almost always means a security policy (AppLocker / Software Restriction Policy) blocks running an executable from %TEMP% -- not a broken download." -ForegroundColor Red
			}
			Write-Error "install.ps1: refusing to install. The Path was not modified. This is a policy on this host, NOT evidence the download is broken -- the sha256 check above already proved the bytes match the release. Ask an administrator to allow execution from $Dir, or install from a checkout instead: git clone https://github.com/$repo && pip install ./tan-cli/python"
		} else {
			Write-Error "install.ps1: refusing to install. The Path was not modified. This host may be missing a runtime dependency the binary needs, or security software may have altered it. Install from a checkout instead: git clone https://github.com/$repo && pip install ./tan-cli/python"
		}
		exit 1
	}

	# tan-cli#803: the REAL, unconditional creation of $Dir -- deferred all the
	# way from immediately after argument parsing (see the $dirFirstNewAncestor
	# comment near the top) to HERE, now that the health check just above has
	# actually PASSED and an install is genuinely going ahead. Every refusal
	# above -- checksums fetch, asset selection, download, sha256 mismatch, bad
	# archive layout, and the health check itself -- now exits before this line
	# ever runs, so none of them can leave $Dir (or any parent it would have
	# created) behind any more. -Force makes this the ps1 equivalent of
	# `mkdir -p`, so it is a harmless no-op if the noexec-retry block above
	# already created $Dir (or it already existed from a previous install).
	New-Item -ItemType Directory -Force -Path $Dir | Out-Null

	# -------------------------------------------------------------------------
	# Commit: back up whatever is already at $destCmd/$destExe/$LibDir, swap the
	# verified payload into place, and if any step fails, put the backup right
	# back -- a failed upgrade must leave the user exactly where they started,
	# never with neither binary (tan-cli#434). This also folds in the old
	# "stale shadow" removal: the two names SHADOW each other on PATH
	# (cmd.exe/PowerShell resolve a bare `tan` by walking PATHEXT in order,
	# .COM;.EXE;.BAT;.CMD by default, so a leftover tan.exe always beats a
	# tan.cmd sitting beside it) -- whichever one is not being installed is
	# backed up and only discarded once the new install is proven in place,
	# never deleted up front.
	#
	# The three renames below and the payload swap that follows are ONE
	# critical section, all inside the same try below (tan-cli#794): a
	# Move-Item failing partway through the BACKUP renames is exactly as
	# dangerous as one failing while placing the new payload -- either way the
	# user can be left without a `tan` on PATH -- so both must land in the
	# same $commitError/Restore-Previous rollback path rather than the backup
	# renames running bare ahead of the only try/catch in this script.
	# -------------------------------------------------------------------------
	$destCmdBak = "$destCmd.bak"
	$destExeBak = "$destExe.bak"
	$libDirBak = "$LibDir.bak"
	$hadDestCmdBackup = $false
	$hadDestExeBackup = $false
	$hadLibBackup = $false
	$backupsDone = $false

	function Restore-Previous {
		# Restores every backup taken below; returns whether it fully succeeded.
		# Needs $hadDestCmdBackup/$hadDestExeBackup/$hadLibBackup/$backupsDone,
		# which is why it is defined after those are initialized -- PowerShell
		# resolves a function's body at CALL time, not definition time, so
		# where this sits relative to the backup renames below is otherwise
		# inert. What actually makes this reachable from a throw during the
		# very first backup rename is the try/catch boundary around both the
		# backup renames AND the payload swap below (tan-cli#794).
		#
		# The three removes below are gated on $backupsDone (tan-cli#794
		# review, finding 1): Restore-Previous is now reachable mid-way
		# through the backup renames themselves, and at that point whatever
		# still sits at $destCmd/$destExe/$LibDir is the UN-BACKED-UP previous
		# install, not new payload -- deleting it unconditionally would
		# destroy the very thing this function exists to save. Only once
		# $backupsDone is set (all three backup renames completed) is
		# anything at those paths guaranteed to be new payload, safe to clear
		# before moving the backups back.
		$ok = $true
		if ($backupsDone) {
			Remove-Item -LiteralPath $destCmd -Force -ErrorAction SilentlyContinue
			Remove-Item -LiteralPath $destExe -Force -ErrorAction SilentlyContinue
			Remove-Item -LiteralPath $LibDir -Recurse -Force -ErrorAction SilentlyContinue
		}
		if ($hadDestCmdBackup) {
			try { Move-Item -LiteralPath $destCmdBak -Destination $destCmd -Force } catch { $ok = $false }
		}
		if ($hadDestExeBackup) {
			try { Move-Item -LiteralPath $destExeBak -Destination $destExe -Force } catch { $ok = $false }
		}
		if ($hadLibBackup) {
			try { Move-Item -LiteralPath $libDirBak -Destination $LibDir -Force } catch { $ok = $false }
		}
		return $ok
	}

	# tan-cli#490 -- Move-Item preserves the SOURCE item's ACL rather than
	# picking up the destination's: $stage/$tmp were extracted/downloaded
	# unprivileged into the invoking user's own %TEMP%, so every file under
	# them carries that user's ACEs, and Move-Item carries them straight into
	# $LibDir/$dest -- under -System that leaves a machine-wide install under
	# %ProgramFiles% writable by an unprivileged user, a
	# local-privilege-escalation shape (anything that later runs the
	# installed `tan` elevated, or another admin, executes what that user can
	# freely overwrite). Applied in both scopes, not just -System: a moved-in
	# ACE is wrong regardless of where it lands.
	#
	# `icacls /reset`, not a manual Get-Acl/SetAccessRuleProtection/Set-Acl:
	# a moved item's ACEs keep whatever "inherited" flag they had FROM THE OLD
	# PARENT -- Windows does not re-evaluate that flag on a move -- so merely
	# re-enabling inheritance (SetAccessRuleProtection($false, $false)) adds
	# the new parent's ACEs ALONGSIDE the stale ones rather than replacing
	# them; the stale entries, inherited-flagged or not, survive either way.
	# `icacls /reset` clears every ACE the item currently carries and
	# re-enables inheritance in one step, so what is left is only what the
	# NEW parent grants -- exactly as if the item had been created there
	# fresh. /T recurses (ignored on a bare file); /C keeps going past any
	# single per-file failure instead of aborting the rest of the tree.
	# Non-fatal and warns rather than rolls back: an install that already
	# succeeded must not be turned into a failure over a hardening step (e.g.
	# a filesystem that does not support ACLs at all). Wrapped in its own
	# try/catch, not just an exit-code check: this whole script runs under
	# $ErrorActionPreference = "Stop", and PowerShell 7.4+ defaults
	# $PSNativeCommandUseErrorActionPreference to $true, which turns a
	# non-zero exit from a NATIVE command (icacls, here) into a terminating
	# exception under that preference -- without this catch, a single denied
	# ACE reset would abort the whole script on a 7.4+ host, past the point an
	# already-successful install had committed and BEFORE the Path update
	# below ever runs.
	function Reset-InheritedAcl([string]$root) {
		try {
			$icaclsOut = & icacls $root /reset /T /C 2>&1
			if ($LASTEXITCODE -ne 0) {
				Write-Host "install.ps1: warning -- could not reset the ACL on $root to inherit from its new parent; it may remain writable by whichever account ran this installer. ($icaclsOut)" -ForegroundColor Yellow
			}
		} catch {
			Write-Host "install.ps1: warning -- could not reset the ACL on $root to inherit from its new parent; it may remain writable by whichever account ran this installer. ($($_.Exception.Message))" -ForegroundColor Yellow
		}
	}

	# Captured before the try below (tan-cli#794 review, finding 3): ground
	# truth about whether a previous install existed at all, independent of
	# how far the backup/commit phase below got. Selecting the "no previous
	# installation existed" message off the $had*Backup flags instead (as
	# before) was wrong the moment the very first backup rename itself threw
	# -- $destCmd could Test-Path true one line above and still leave every
	# $had*Backup flag $false, since the flag is only set AFTER its rename
	# succeeds.
	$hadAnyPrevious = (Test-Path -LiteralPath $destCmd) -or (Test-Path -LiteralPath $destExe) -or (Test-Path -LiteralPath $LibDir)

	$commitError = $null
	try {
		if (Test-Path -LiteralPath $destCmd) {
			Remove-Item -LiteralPath $destCmdBak -Force -ErrorAction SilentlyContinue
			Move-Item -LiteralPath $destCmd -Destination $destCmdBak -Force
			$hadDestCmdBackup = $true
		}
		if (Test-Path -LiteralPath $destExe) {
			Remove-Item -LiteralPath $destExeBak -Force -ErrorAction SilentlyContinue
			Move-Item -LiteralPath $destExe -Destination $destExeBak -Force
			$hadDestExeBackup = $true
		}
		if (Test-Path -LiteralPath $LibDir) {
			Remove-Item -LiteralPath $libDirBak -Recurse -Force -ErrorAction SilentlyContinue
			Move-Item -LiteralPath $LibDir -Destination $libDirBak -Force
			$hadLibBackup = $true
		}
		# All three backup renames completed -- anything still at $destCmd/
		# $destExe/$LibDir from here on is new payload this run itself places,
		# never the previous install (tan-cli#794 review, finding 1).
		$backupsDone = $true

		if ($layout -eq "archive") {
			Move-Item -LiteralPath (Join-Path $stage "tan") -Destination $LibDir -Force

			# A thin launcher, not a symlink (symlinks need elevation/Developer Mode
			# on Windows by default and would not survive `-System` cleanly either):
			# a .cmd, because PATHEXT resolves `tan` to it the same way it would an
			# .exe, and it gives a future reader somewhere obvious to add a wrapper
			# concern without editing the generated tree in place. `%~dp0` (the
			# launcher's own directory) rather than a baked-in absolute path, so the
			# launcher keeps working if $Dir is ever relocated as a unit.
			$launcherContent = @'
@echo off
rem Generated by tan install.ps1 (tan-cli#349) -- do not edit by hand.
rem Re-run install.ps1 to update both this launcher and %~dp0tan-cli-lib.
"%~dp0tan-cli-lib\tan.exe" %*
exit /b %ERRORLEVEL%
'@
			# ASCII, no BOM: a BOM ahead of `@echo off` corrupts cmd.exe's parse of
			# the first line on some Windows builds.
			Set-Content -LiteralPath $dest -Value $launcherContent -Encoding ascii -NoNewline
		} else {
			Move-Item -LiteralPath $tmp -Destination $dest -Force
		}
	} catch {
		$commitError = $_.Exception.Message
	}

	if ($commitError) {
		if ($backupsDone) {
			Write-Host "install.ps1: failed to place the new install under $Dir -- rolling back: $commitError" -ForegroundColor Red
		} else {
			Write-Host "install.ps1: failed to back up the previous install under $Dir before placing the new one -- rolling back: $commitError" -ForegroundColor Red
		}
		$restored = Restore-Previous
		if (-not $hadAnyPrevious) {
			Write-Host "install.ps1: no previous installation existed; nothing to restore. Install failed." -ForegroundColor Red
		} elseif ($restored) {
			Write-Host "install.ps1: previous installation restored -- 'tan' still works as before. Install failed." -ForegroundColor Red
		} else {
			Write-Host "install.ps1: WARNING -- could not fully restore the previous installation. Backups remain at $destCmdBak / $destExeBak / $libDirBak (whichever existed) -- move them back by hand." -ForegroundColor Red
		}
		Write-Error "install.ps1: the Path was not modified."
		exit 1
	}

	# Commit succeeded -- reset the ACL on whatever was actually MOVED into
	# place (see Reset-InheritedAcl above). $dest under the archive layout is
	# excluded: it was written fresh via Set-Content directly at its final
	# home, never moved, so it already inherits $Dir's ACL correctly.
	if ($layout -eq "archive") {
		Reset-InheritedAcl $LibDir
	} else {
		Reset-InheritedAcl $dest
	}

	# Backups (including any stale other-layout dest, which would otherwise
	# shadow $dest on PATH per PATHEXT order) are no longer needed.
	Remove-Item -LiteralPath $destCmdBak -Force -ErrorAction SilentlyContinue
	Remove-Item -LiteralPath $destExeBak -Force -ErrorAction SilentlyContinue
	Remove-Item -LiteralPath $libDirBak -Recurse -Force -ErrorAction SilentlyContinue
} finally {
	# One wildcard over the shared GUID stem: $tmp/$sumsTmp/$stage all hang off
	# $tmpBase, and $tmp may not even be assigned yet if the selection above
	# refused (Set-StrictMode makes naming an unassigned variable a throw, which
	# inside `finally` would mask the real error).
	Remove-Item -Path "$tmpBase*" -Recurse -Force -ErrorAction SilentlyContinue
}

# Add $Dir to the chosen PATH scope if absent. Machine scope requires admin.
# Reached only after the commit above succeeded, so a failed install never
# leaves a Path edit behind (tan-cli#434).
#
# tan-cli#490 -- the worst defect in the whole report: [Environment]::
# GetEnvironmentVariable("Path", $scope) EXPANDS a REG_EXPAND_SZ Path value
# (e.g. "%JAVA_HOME%\bin;%LOCALAPPDATA%\Microsoft\WindowsApps") before
# returning it, and [Environment]::SetEnvironmentVariable always writes back
# REG_SZ -- so that round-trip SILENTLY AND PERMANENTLY collapses every %VAR%
# reference in the User (or, under -System, the MACHINE) Path to its
# expansion at install time. If the profile is ever relocated (domain join,
# roaming profile, a drive-letter change) every collapsed entry breaks, with
# nothing pointing at tan's installer as the cause. This is exactly why
# rustup/scoop/chocolatey open the registry directly rather than go through
# these two .NET calls: read UNEXPANDED (DoNotExpandEnvironmentNames), and
# write back using the SAME ValueKind the key already had, so a REG_EXPAND_SZ
# Path stays REG_EXPAND_SZ and a REG_SZ one stays REG_SZ.
function Get-PathRegistryKey([bool]$writable, [string]$scopeName = $scope) {
	# Test-only seam (tan-cli#490): the REG_EXPAND_SZ-preserving read/write
	# below is exactly the code this whole defect is about, and every existing
	# test runs with -NoModifyPath, so none of them ever reach it -- the one
	# path that most needs coverage was the one path no test could exercise
	# without either permanently rewriting the machine running the test suite
	# (the real User key) or needing elevation the CI runner does not have
	# (the real Machine key). Pointing this at a scratch HKCU subkey instead
	# exercises the identical registry mechanics (read UNEXPANDED, write back
	# with the SAME ValueKind) without touching the real Environment key in
	# either hive. Never set outside the test suite.
	#
	# tan-cli#678: $scopeName defaults to the install's own -System/-User
	# $scope (every pre-existing call site), but the PATH-shadow check below
	# passes "Machine"/"User" explicitly -- it has to read BOTH hives
	# regardless of which one this install just wrote to, since a new shell's
	# PATH is Machine THEN User, not whichever single scope -System selected.
	if ($env:TAN_INSTALL_TEST_PATH_REGISTRY_KEY) {
		return [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey(
			$env:TAN_INSTALL_TEST_PATH_REGISTRY_KEY, $writable)
	}
	if ($scopeName -eq "Machine") {
		return [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey(
			"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", $writable)
	} else {
		return [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment", $writable)
	}
}
# Read-only first: reading either hive's Environment key needs no elevation,
# and -NoModifyPath must keep working exactly as before even when this
# process is not elevated for -System (only the WRITE below needs that).
$pathKey = Get-PathRegistryKey $false
try {
	$curPathRaw = $pathKey.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
	try {
		$curKind = $pathKey.GetValueKind("Path")
	} catch {
		# No Path value on this key at all yet (rare, but possible on a fresh
		# account) -- REG_EXPAND_SZ is the kind Windows itself uses for a new
		# Path, so a value this script creates matches that convention.
		$curKind = [Microsoft.Win32.RegistryValueKind]::ExpandString
	}
} finally {
	$pathKey.Close()
}
# Compare both the raw entry and its expansion: an existing entry may have
# been written unexpanded (e.g. "%LOCALAPPDATA%\Programs\tan") by another
# REG_EXPAND_SZ-aware installer, and must still count as "already present"
# even though $Dir here is always the expanded, literal form.
$alreadyPresent = $curPathRaw -split ';' | Where-Object {
	$_ -eq $Dir -or [Environment]::ExpandEnvironmentVariables($_) -eq $Dir
}
if (-not $alreadyPresent) {
	if ($NoModifyPath) {
		Write-Host "install.ps1: $Dir is not on the $scope Path -- add it yourself, or re-run without -NoModifyPath."
	} else {
		$pathKey = Get-PathRegistryKey $true
		try {
			$newPath = if ([string]::IsNullOrEmpty($curPathRaw)) { $Dir } else { "$curPathRaw;$Dir" }
			$pathKey.SetValue("Path", $newPath, $curKind)
		} finally {
			$pathKey.Close()
		}
		# Best-effort, like Reset-InheritedAcl above: the registry write just
		# above already committed, so a broadcast that cannot run must not
		# turn an install that already succeeded into a failure. `Add-Type`
		# compiles this class into a temp assembly it then loads -- which an
		# AppLocker DLL rule or a Software Restriction Policy over %TEMP%
		# (the exact host class the health-check retry above exists for) can
		# block. `-ErrorAction SilentlyContinue` on `Add-Type` only suppresses
		# a NON-terminating error; a compile/assembly-load failure from
		# `Add-Type` itself is a TERMINATING one, so without a try/catch of
		# its own it would abort the whole script here -- with the Path
		# already committed to the registry -- turning a should-be-cosmetic
		# broadcast failure into a reported install failure. WM_SETTINGCHANGE,
		# done by hand: [Environment]::SetEnvironmentVariable sends this
		# itself (target User/Machine) so already-running GUI apps (Explorer,
		# and consoles it spawns afterward) pick up the change without a
		# reboot -- a direct registry write bypasses that side effect
		# entirely, so it has to be replicated here or a working installer
		# would silently start requiring a logoff/logon it never used to.
		try {
			Add-Type -Namespace TanInstall -Name NativeMethods -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(
	IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam,
	uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@ -ErrorAction SilentlyContinue
		} catch {
			Write-Host "install.ps1: warning -- could not compile the Path-broadcast helper ($($_.Exception.Message)); open a NEW terminal to pick it up." -ForegroundColor Yellow
		}
		# Gate on the type actually existing (Add-Type above may have been
		# unable to define it), and wrap the call itself too in case the
		# broadcast fails for some other reason.
		if ('TanInstall.NativeMethods' -as [type]) {
			try {
				$result = [UIntPtr]::Zero
				[TanInstall.NativeMethods]::SendMessageTimeout(
					[IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, "Environment", 2, 5000, [ref]$result) | Out-Null
			} catch {
				Write-Host "install.ps1: warning -- could not broadcast the Path change to running apps ($($_.Exception.Message)); open a NEW terminal to pick it up." -ForegroundColor Yellow
			}
		}
		Write-Host "install.ps1: added $Dir to the $scope Path -- restart the terminal for it to take effect."
	}
}

if ($layout -eq "archive") {
	Write-Host "install.ps1: installed tan -> $dest (runtime: $LibDir)"
} else {
	Write-Host "install.ps1: installed tan -> $dest"
}

# -----------------------------------------------------------------------
# tan-cli#678: the health check above proves the STAGED binary runs; it says
# nothing about whether it is the `tan` a NEW shell actually resolves. On a
# host that already has a different `tan` earlier on PATH, this install is
# real -- checksum verified, health check passed, $dest is exactly what was
# promised -- but invisible: the next shell runs the OTHER one, and this
# script would otherwise never say so. One warning line. This never reorders
# PATH or removes the other binary (that would be hostile), and it never
# fails the install over it -- the install DID succeed, so $LASTEXITCODE
# stays 0 either way.
#
# Fires ONLY when BOTH hold:
#   1. $Dir is on the EFFECTIVE Path a NEW session will get -- Machine THEN
#      User, read fresh from the registry (never this process's own
#      $env:Path, which can be stale) -- mirrors the issue's own
#      re-resolution methodology exactly. When $Dir is not on that Path at
#      all (-NoModifyPath and declined, or the user chose not to modify it),
#      the "is not on the ... Path -- add it yourself" message above already
#      covers that state; warning here too would be noise about something
#      already reported. $alreadyPresent / -not $NoModifyPath (from the Path
#      block above) already answer this without a second read of $scope's
#      own key.
#   2. Resolving "tan" against that effective Path + PATHEXT order -- the
#      same directory-major, extension-minor order cmd.exe/PowerShell
#      actually use -- lands on something other than $dest. Compared by
#      RESOLVED, case-insensitive path (symlink/8.3 short name normalised
#      first) -- never by version: two legitimate installs can share a
#      version, and comparing versions would mean running the other binary
#      just to decide whether to warn at all.
#
# The whole check is wrapped in try/catch: a bug here (a locked-down host
# that cannot even read the OTHER hive, a shadowing binary that refuses to
# run) must never turn an install that already succeeded into a reported
# failure.
# -----------------------------------------------------------------------
try {
	$dirIsOnEffectivePath = $alreadyPresent -or (-not $NoModifyPath)
	if ($dirIsOnEffectivePath) {
		function Get-ScopeRawPathDirs([string]$scopeName) {
			$key = Get-PathRegistryKey $false $scopeName
			if (-not $key) { return @() }
			try {
				$raw = $key.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
			} finally {
				$key.Close()
			}
			if (-not $raw) { return @() }
			return ($raw -split ';' | Where-Object { $_ } | ForEach-Object { [Environment]::ExpandEnvironmentVariables($_) })
		}
		# The Path write above (if it ran) already computed $newPath for
		# $scope's own key -- use that in-memory value instead of a stale
		# re-read, so a run that just added $Dir sees itself on the list
		# without needing the registry write to have already landed/synced.
		$justWroteNewPath = (-not $alreadyPresent) -and (-not $NoModifyPath)
		if ($scope -eq "Machine") {
			$machineDirs = if ($justWroteNewPath) { ($newPath -split ';' | Where-Object { $_ } | ForEach-Object { [Environment]::ExpandEnvironmentVariables($_) }) } else { Get-ScopeRawPathDirs "Machine" }
			$userDirs = Get-ScopeRawPathDirs "User"
		} else {
			$machineDirs = Get-ScopeRawPathDirs "Machine"
			$userDirs = if ($justWroteNewPath) { ($newPath -split ';' | Where-Object { $_ } | ForEach-Object { [Environment]::ExpandEnvironmentVariables($_) }) } else { Get-ScopeRawPathDirs "User" }
		}
		# Machine THEN User -- the same order Windows composes a new session's
		# PATH in, not an arbitrary one.
		$effectiveDirs = @($machineDirs) + @($userDirs)

		function Find-FirstTanOnPath([string[]]$dirs) {
			$pathext = if ($env:PATHEXT) { $env:PATHEXT } else { ".COM;.EXE;.BAT;.CMD" }
			$exts = @($pathext -split ';' | Where-Object { $_ })
			foreach ($d in $dirs) {
				if (-not $d) { continue }
				foreach ($ext in $exts) {
					$candidate = Join-Path $d ("tan" + $ext)
					if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
				}
			}
			return $null
		}

		function Resolve-CanonicalPath([string]$path) {
			# Best-effort normalisation: GetFullPath first (case/relative/
			# redundant-separator differences), then follow a symlink/junction
			# target if Get-Item reports the LEAF ITSELF is one, then try to
			# expand any 8.3 short name via GetLongPathName. Each stage is
			# independently guarded -- a stage that cannot run just leaves the
			# path as the previous stage left it, never throws out of this
			# function. Does not chase a symlink on an ANCESTOR directory (only
			# on the leaf itself) -- there is no cheap realpath(3) equivalent
			# in .NET, and under-resolving two really-identical paths into a
			# spurious warning is the safe failure direction to take here.
			$full = [System.IO.Path]::GetFullPath($path)
			try {
				$item = Get-Item -LiteralPath $full -ErrorAction Stop
				if ($item.PSObject.Properties['LinkType'] -and $item.LinkType -and $item.Target) {
					$target = @($item.Target)[0]
					if ($target) {
						if (-not [System.IO.Path]::IsPathRooted($target)) {
							$target = Join-Path (Split-Path $full -Parent) $target
						}
						$full = [System.IO.Path]::GetFullPath($target)
					}
				}
			} catch { }
			try {
				if (-not ('TanInstall.PathNative' -as [type])) {
					Add-Type -Namespace TanInstall -Name PathNative -MemberDefinition @'
[DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
public static extern uint GetLongPathName(string lpszShortPath, System.Text.StringBuilder lpszLongPath, uint cchBuffer);
'@ -ErrorAction SilentlyContinue
				}
				if ('TanInstall.PathNative' -as [type]) {
					$sb = New-Object System.Text.StringBuilder 1024
					$len = [TanInstall.PathNative]::GetLongPathName($full, $sb, [uint32]$sb.Capacity)
					if ($len -gt 0 -and $len -lt $sb.Capacity) { $full = $sb.ToString() }
				}
			} catch { }
			return $full
		}

		function Get-TanVersionReport([string]$exePath) {
			# Best-effort, with a real timeout: this runs a binary the
			# installer does not control (whatever was already on PATH), and
			# a hang there must not hang the install. Output is read via the
			# async Read*Async APIs, started BEFORE WaitForExit, so a chatty
			# process cannot deadlock this on a full pipe buffer.
			try {
				$psi = New-Object System.Diagnostics.ProcessStartInfo
				$psi.FileName = $exePath
				$psi.Arguments = "--version"
				$psi.RedirectStandardOutput = $true
				$psi.RedirectStandardError = $true
				$psi.UseShellExecute = $false
				$psi.CreateNoWindow = $true
				$proc = [System.Diagnostics.Process]::Start($psi)
				$stdoutTask = $proc.StandardOutput.ReadToEndAsync()
				$stderrTask = $proc.StandardError.ReadToEndAsync()
				if ($proc.WaitForExit(5000)) {
					[System.Threading.Tasks.Task]::WaitAll(@($stdoutTask, $stderrTask), 1000) | Out-Null
					$out = ($stdoutTask.Result + $stderrTask.Result).Trim()
					if ($out) { return $out }
				} else {
					try { $proc.Kill() } catch { }
				}
			} catch { }
			return "could not run"
		}

		$winner = Find-FirstTanOnPath $effectiveDirs
		if ($winner) {
			$winnerCanon = Resolve-CanonicalPath $winner
			$destCanon = Resolve-CanonicalPath $dest
			if ($winnerCanon -ine $destCanon) {
				$shadowVersion = Get-TanVersionReport $winner
				Write-Host "install.ps1: WARNING: another tan is earlier on PATH and will shadow this install:" -ForegroundColor Yellow
				Write-Host "  $winner  (reports: $shadowVersion)" -ForegroundColor Yellow
				Write-Host "  installed here: $dest  ($verifyOut)" -ForegroundColor Yellow
			}
		}
	}
} catch {
	# Non-fatal, deliberately (tan-cli#678): a bug in this best-effort warning
	# must never fail an install that already succeeded.
}
