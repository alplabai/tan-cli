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
TAN_VERSION = "0.5.0-dev"
