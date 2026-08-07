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
#   * Every tag published BEFORE v0.5.0 -- including v0.4.1, which is what
#     `latest` resolves to today, and the v0.5.0-rc4 pre-release -- publishes a
#     raw tan.exe. That one is installed as $Dir\tan.exe, no launcher and no
#     tan-cli-lib\.
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

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
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
	# tag published before it -- v0.4.1, which is what `latest` resolves to
	# today, and the v0.5.0-rc4 pre-release -- publishes a raw tan.exe under the
	# same triple. Requesting the .zip unconditionally 404s on every tag that
	# exists right now, which is what #356 reported.
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
	# -------------------------------------------------------------------------
	function Test-AccessDeniedSignature([string]$message) {
		return $message -match "Access is denied"
	}
	function Invoke-HealthCheck([string]$exePath) {
		try {
			$out = (& $exePath --version 2>&1 | Out-String).Trim()
			return @{ Out = $out; Exit = $LASTEXITCODE }
		} catch {
			return @{ Out = $_.Exception.Message; Exit = 1 }
		}
	}

	$check = Invoke-HealthCheck $stagedExe
	$verifyOut = $check.Out
	$verifyExit = $check.Exit

	$retried = $false
	if ($verifyExit -ne 0 -and (Test-AccessDeniedSignature $verifyOut)) {
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
		} catch {
			# $Dir could not be used for the retry (e.g. -System without
			# elevation) -- fall through to the failure branch below with the
			# ORIGINAL %TEMP% result.
		} finally {
			Remove-Item -LiteralPath $retryDir -Recurse -Force -ErrorAction SilentlyContinue
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
		if (Test-AccessDeniedSignature $verifyOut) {
			if ($retried) {
				Write-Host "install.ps1: Access is denied persisted even after staging inside $Dir -- a security policy (AppLocker / Software Restriction Policy) very likely blocks running tan from there too, not only from %TEMP%." -ForegroundColor Red
			} else {
				Write-Host "install.ps1: Access is denied, with the file freshly downloaded and sha256-verified, almost always means a security policy (AppLocker / Software Restriction Policy) blocks running an executable from %TEMP% -- not a broken download. ($Dir could not be used for a retry without elevation.)" -ForegroundColor Red
			}
			Write-Error "install.ps1: refusing to install. The Path was not modified. This is a policy on this host, NOT evidence the download is broken -- the sha256 check above already proved the bytes match the release. Ask an administrator to allow execution from $Dir, or install from a checkout instead: git clone https://github.com/$repo && pip install ./tan-cli/python"
		} else {
			Write-Error "install.ps1: refusing to install. The Path was not modified. This host may be missing a runtime dependency the binary needs, or security software may have altered it. Install from a checkout instead: git clone https://github.com/$repo && pip install ./tan-cli/python"
		}
		exit 1
	}

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
	# -------------------------------------------------------------------------
	$destCmdBak = "$destCmd.bak"
	$destExeBak = "$destExe.bak"
	$libDirBak = "$LibDir.bak"
	$hadDestCmdBackup = $false
	$hadDestExeBackup = $false
	$hadLibBackup = $false
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

	function Restore-Previous {
		# Restores every backup taken above; returns whether it fully succeeded.
		$ok = $true
		Remove-Item -LiteralPath $destCmd -Force -ErrorAction SilentlyContinue
		Remove-Item -LiteralPath $destExe -Force -ErrorAction SilentlyContinue
		Remove-Item -LiteralPath $LibDir -Recurse -Force -ErrorAction SilentlyContinue
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

	$commitError = $null
	try {
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
		Write-Host "install.ps1: failed to place the new install under $Dir -- rolling back: $commitError" -ForegroundColor Red
		$restored = Restore-Previous
		if (-not ($hadDestCmdBackup -or $hadDestExeBackup -or $hadLibBackup)) {
			Write-Host "install.ps1: no previous installation existed; nothing to restore. Install failed." -ForegroundColor Red
		} elseif ($restored) {
			Write-Host "install.ps1: previous installation restored -- 'tan' still works as before. Install failed." -ForegroundColor Red
		} else {
			Write-Host "install.ps1: WARNING -- could not fully restore the previous installation. Backups remain at $destCmdBak / $destExeBak / $libDirBak (whichever existed) -- move them back by hand." -ForegroundColor Red
		}
		Write-Error "install.ps1: the Path was not modified."
		exit 1
	}

	# Commit succeeded -- backups (including any stale other-layout dest, which
	# would otherwise shadow $dest on PATH per PATHEXT order) are no longer
	# needed.
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

# Add $Dir to the chosen PATH scope if absent. Machine scope requires admin;
# SetEnvironmentVariable throws a clear permission error if not elevated.
# Reached only after the commit above succeeded, so a failed install never
# leaves a Path edit behind (tan-cli#434).
$curPath = [Environment]::GetEnvironmentVariable("Path", $scope)
if (-not ($curPath -split ';' | Where-Object { $_ -eq $Dir })) {
	if ($NoModifyPath) {
		Write-Host "install.ps1: $Dir is not on the $scope Path -- add it yourself, or re-run without -NoModifyPath."
	} else {
		$newPath = if ([string]::IsNullOrEmpty($curPath)) { $Dir } else { "$curPath;$Dir" }
		[Environment]::SetEnvironmentVariable("Path", $newPath, $scope)
		Write-Host "install.ps1: added $Dir to the $scope Path -- restart the terminal for it to take effect."
	}
}

if ($layout -eq "archive") {
	Write-Host "install.ps1: installed tan -> $dest (runtime: $LibDir)"
} else {
	Write-Host "install.ps1: installed tan -> $dest"
}
