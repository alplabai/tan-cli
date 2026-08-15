# SPDX-License-Identifier: Apache-2.0
# The Python port ships as tan v0.5.0 (maintainer decision, 2026-07-29).
#
# It must NOT reuse 0.4.0: that is the version of the SHIPPED Rust release, and
# alp-sdk-vscode pins SUPPORTED_CLI_VERSION = "0.4.0" and gates its refetch on
# isCliBehind(), which returns false on equality. A Python binary claiming
# "0.4.0" therefore never triggers an upgrade, while isNativeTanVersionOutput()
# happily ACCEPTS it -- an accepted CLI whose commands do not exist yet, which
# is strictly worse for a user's project than a rejected one.
#
# Pre-1.0 SemVer puts a break in the MINOR, and a language rewrite is a break.
#
# THIS IS A DEVELOPMENT VERSION, and that is the point (tan-cli#377). It sat at
# a flat "0.5.0-rc4" for the whole post-rc4 wave, which is the version of an
# ALREADY-PUBLISHED tag (v0.5.0-rc4 -> fbbea03d): the released binary and every
# build of this tree answered `tan 0.5.0-rc4` while behaving differently, so no
# bug report could name which one it was on. `version_check.py --not-released`
# now refuses that state, so this cannot silently recur next cycle.
#
# Why `-rc5.dev0` and not the obvious `0.5.0-dev`: `dev` sorts BELOW `rc` in
# SemVer AND in PEP 440, so `0.5.0-dev` would sort below the v0.5.0-rc4 this
# tree is built on top of -- a newer build claiming to predate the release it
# came after. `0.5.0-rc5.dev0` sorts rc4 < rc5.dev0 < rc5 < 0.5.0 in both
# ecosystems. It does NOT promise an rc5 will be cut: PEP 440 reads `X.devN` as
# "before X", and the next release being 0.5.0 (above rc5) keeps that true
# either way. Build metadata (`0.5.0-rc4+dev`) was rejected for the opposite
# reason -- SemVer 2.0.0 sec. 10 requires precedence to IGNORE it, so it would
# have sorted EQUAL to the published tag.
#
# At release: drop the pre-release entirely (`0.5.0`), date CHANGELOG.md's
# `## [0.5.0]` heading, and bring python/pyproject.toml (PEP 440 spelling) and
# npm-shim/package.json (this exact string) with it --
# `python/scripts/version_check.py --selftest --self --not-released` checks all
# four together.
#
# 0.5.1: patch fix for the 27 camelCase issue codes v0.5.0 shipped in
# `contract/issue-codes.json` (`doctor.boardYaml`, `support-
# bundle.gdbserverBackend`, ...) -- all `"reserved"`/`"consumer": "none"`, so
# the rename is free. See CHANGELOG.md's `## [0.5.1]` entry.
#
# v0.5.1 published 2026-08-05, so this tree moves on the same day: leaving
# TAN_VERSION at "0.5.1" is the exact state `--not-released` exists to refuse,
# and it turned `version-identity` red on every open PR the moment that tag
# appeared. `0.5.2-rc1.dev0` is the spelling that check prescribes -- a `.devN`
# tail on the NEXT pre-release -- and it promises neither a 0.5.2 nor an rc1:
# PEP 440 reads `X.devN` as "before X", so a 0.6.0 next release still sorts
# above it. Its CHANGELOG home is `## [0.5.2] — Unreleased`, because a
# development version documents the release it heads for, never itself.
#
# v0.6.0-rc1 published 2026-08-14 (tan-cli#764): this is the SAME recurrence,
# one cycle later -- `dev`'s tip carried the published tag's exact version, so
# `--not-released` turned `version-identity` red on every PR opened against
# `dev` (#762, #763), for a reason unrelated to either diff. `0.6.0-rc2.dev0`
# is the same spelling this file has used twice before -- a `.devN` tail on
# the NEXT pre-release -- and carries the same non-promise: PEP 440 reads
# `X.devN` as "before X", so neither an actual rc2 nor the eventual 0.6.0 is
# guaranteed by this string, only that both sort above it. CHANGELOG home is
# `## [0.6.0] — Unreleased`, per `release_target()`.
TAN_VERSION = "0.6.0-rc2.dev0"
