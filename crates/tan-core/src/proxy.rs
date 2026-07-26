// SPDX-License-Identifier: Apache-2.0
//! Which proxy environment variable applies to an `https://` request, and
//! whether `NO_PROXY` exempts the target from it.
//!
//! Pure on purpose. Reading the environment is IO and lives in `tan-cli`
//! (`http::env_proxy`); the *precedence rule* is a decision, and a decision
//! with a scheme trap in it deserves a test that does not need to mutate the
//! process environment (`std::env::set_var` is `unsafe` in edition 2024, and a
//! cargo test binary is multi-threaded — a set_var-based test is a data race
//! waiting to flake).
//!
//! **The trap.** ureq 2.12's own `try_proxy_from_env` reads
//! `ALL_PROXY`/`HTTPS_PROXY`/`HTTP_PROXY` *regardless of the target URL scheme*
//! (`proxy.rs` `try_env!`), and then applies whatever it found unconditionally
//! (`stream.rs`). So a host that exports only `HTTP_PROXY` — the conventional
//! "proxy for plain-HTTP URLs only" setting, and a common corporate one — would
//! have its `https://api.github.com` request pushed through a proxy that may
//! well refuse `CONNECT`, breaking a machine that worked fine going direct.
//!
//! curl, git and Python's `urllib` all treat `HTTP_PROXY` as http-only, so
//! matching them is the choice that surprises a corporate Windows host least:
//! a box that genuinely proxies TLS sets `HTTPS_PROXY` (or `ALL_PROXY`) too.
//! We therefore select the proxy ourselves instead of using
//! `try_proxy_from_env(true)`.
//!
//! **The other half of that same trap is `NO_PROXY`.** ureq 2.12 has no support
//! for it, so honouring the proxy env without it would take a corporate host
//! that exports `HTTPS_PROXY` *and* a `NO_PROXY` covering GitHub from working
//! direct to proxied — the exact "we broke a machine that worked" shape the
//! `HTTP_PROXY` rule above exists to avoid, aimed at the very population this
//! module is for. So the bypass list is applied here, against the target host.

/// Proxy env vars that apply to an `https://` URL, in the precedence order ureq
/// itself uses (`ALL_PROXY` first, lowercase alias immediately after its
/// uppercase form). `HTTP_PROXY`/`http_proxy` are deliberately absent — see the
/// module docs.
pub const HTTPS_PROXY_ENV_VARS: [&str; 4] =
    ["ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"];

/// Bypass-list env vars, uppercase first for the same reason as above. curl and
/// git read exactly these two.
pub const NO_PROXY_ENV_VARS: [&str; 2] = ["NO_PROXY", "no_proxy"];

/// The proxy URL an `https://` request to `target_url` should go through, or
/// `None` for direct.
///
/// `lookup` is the injected environment read. An empty value counts as unset:
/// exporting `HTTPS_PROXY=` is the conventional way to *disable* an inherited
/// proxy, and treating it as a proxy URL would make every request fail.
///
/// Takes the whole URL rather than a pre-extracted host so that deriving the
/// host is covered by this module's tests too: doing it at the (IO) call site
/// would put an untested string-split between the rule and the request, which
/// is precisely the seam this pure module exists to remove.
pub fn select_https_proxy(
    target_url: &str,
    lookup: impl Fn(&str) -> Option<String>,
) -> Option<String> {
    let first_set = |keys: &[&str]| {
        keys.iter()
            .find_map(|key| lookup(key).filter(|value| !value.trim().is_empty()))
    };
    let proxy = first_set(HTTPS_PROXY_ENV_VARS.as_slice())?;
    match first_set(NO_PROXY_ENV_VARS.as_slice()) {
        Some(list) if no_proxy_covers(&list, &host_of(target_url)) => None,
        _ => Some(proxy),
    }
}

/// The host of an absolute URL — no scheme, userinfo, port, path or trailing
/// root dot — lowercased for comparison.
///
/// Deliberately not a URL parser: every URL that reaches this crate's HTTP path
/// is one we wrote ourselves ([`crate::GITHUB_RELEASES_URL`]), so a `url`
/// dependency would be more moving parts than the two splits it replaces.
fn host_of(url: &str) -> String {
    let after_scheme = url.split_once("://").map_or(url, |(_, rest)| rest);
    let authority = after_scheme
        .split(['/', '?', '#'])
        .next()
        .unwrap_or_default();
    let host = authority.rsplit_once('@').map_or(authority, |(_, h)| h);
    host.split(':')
        .next()
        .unwrap_or_default()
        .trim_end_matches('.')
        .to_ascii_lowercase()
}

/// Does a `NO_PROXY` list cover `host` (already lowercased by [`host_of`])?
///
/// The rules are curl's/git's/Python's, because those are what the user's
/// existing `NO_PROXY` was written against:
///
/// * `*` as an entry bypasses the proxy for everything.
/// * The list is comma-separated; surrounding whitespace and empty entries are
///   ignored, so `"a, ,b"` is two entries and a trailing comma is harmless.
/// * Matching is case-insensitive on both sides — DNS is.
/// * An entry matches the host exactly, or as a suffix **on a label boundary**:
///   `github.com` and `.github.com` both cover `api.github.com`, while
///   `hub.com` covers neither. A bare `ends_with` is the classic bug here — it
///   would let `hub.com` silently disable the proxy for `github.com`.
/// * A `:port` on an entry is ignored rather than honoured: every request on
///   this path is `https://` on 443, so a port-qualified entry can only mean
///   443 anyway, and *not* bypassing because the user wrote `github.com:443`
///   would be the surprising half of the two.
fn no_proxy_covers(list: &str, host: &str) -> bool {
    list.split(',').any(|entry| {
        let entry = entry.trim();
        if entry == "*" {
            return true;
        }
        let entry = entry
            .split(':')
            .next()
            .unwrap_or_default()
            .trim_matches('.')
            .to_ascii_lowercase();
        !entry.is_empty()
            && (host == entry || host.strip_suffix(&entry).is_some_and(|r| r.ends_with('.')))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The one URL this crate's in-process HTTP path ever requests, so every
    /// case below is stated against the host a real user's `NO_PROXY` would be
    /// judged on rather than a made-up one.
    const TARGET: &str = crate::GITHUB_RELEASES_URL;

    /// Lookup over a fixed table, so each case states exactly the environment it
    /// means and nothing leaks between tests.
    fn env<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + use<'a> {
        move |key| {
            pairs
                .iter()
                .find(|(k, _)| *k == key)
                .map(|(_, v)| (*v).to_string())
        }
    }

    /// `HTTPS_PROXY` set plus whatever bypass list a case is about — the only
    /// shape in which `NO_PROXY` can change an answer.
    fn with_no_proxy(list: &str) -> Option<String> {
        select_https_proxy(
            TARGET,
            env(&[("HTTPS_PROXY", "http://corp:8080"), ("NO_PROXY", list)]),
        )
    }

    #[test]
    fn all_proxy_outranks_https_proxy() {
        let chosen = select_https_proxy(
            TARGET,
            env(&[
                ("HTTPS_PROXY", "http://corp:8080"),
                ("ALL_PROXY", "socks5://127.0.0.1:1080"),
            ]),
        );
        assert_eq!(chosen.as_deref(), Some("socks5://127.0.0.1:1080"));
    }

    #[test]
    fn uppercase_outranks_its_lowercase_alias() {
        let chosen = select_https_proxy(
            TARGET,
            env(&[
                ("https_proxy", "http://lower:8080"),
                ("HTTPS_PROXY", "http://upper:8080"),
            ]),
        );
        assert_eq!(chosen.as_deref(), Some("http://upper:8080"));
    }

    #[test]
    fn http_proxy_alone_leaves_an_https_request_direct() {
        // The regression this whole module exists to prevent: ureq's own env
        // reader would proxy through this, and a plain-HTTP-only proxy refusing
        // CONNECT then breaks a host that worked.
        assert_eq!(
            select_https_proxy(
                TARGET,
                env(&[
                    ("HTTP_PROXY", "http://corp:8080"),
                    ("http_proxy", "http://corp:8080"),
                ])
            ),
            None
        );
    }

    #[test]
    fn an_empty_value_means_unset_and_falls_through() {
        // `HTTPS_PROXY=` is how a shell disables an inherited proxy; it must not
        // shadow a real `ALL_PROXY`, and on its own it must not be a proxy URL.
        assert_eq!(
            select_https_proxy(
                TARGET,
                env(&[("ALL_PROXY", "   "), ("HTTPS_PROXY", "http://corp:8080")])
            )
            .as_deref(),
            Some("http://corp:8080")
        );
        assert_eq!(
            select_https_proxy(TARGET, env(&[("HTTPS_PROXY", "")])),
            None
        );
    }

    #[test]
    fn nothing_set_is_direct() {
        assert_eq!(select_https_proxy(TARGET, env(&[])), None);
    }

    #[test]
    fn no_proxy_naming_the_host_exactly_wins_over_the_proxy() {
        // The case that motivated this: a corporate box exports HTTPS_PROXY for
        // the internet at large and exempts GitHub. Ignoring the exemption sends
        // it through the proxy, i.e. breaks a machine that worked direct.
        assert_eq!(with_no_proxy("api.github.com"), None);
    }

    #[test]
    fn no_proxy_matches_a_parent_domain_with_or_without_a_leading_dot() {
        // `.github.com` is the traditional spelling and `github.com` the modern
        // one; curl, git and Python accept both, so a user's list uses either.
        assert_eq!(with_no_proxy(".github.com"), None);
        assert_eq!(with_no_proxy("github.com"), None);
    }

    #[test]
    fn no_proxy_wildcard_bypasses_everything() {
        assert_eq!(with_no_proxy("*"), None);
    }

    #[test]
    fn a_port_qualified_no_proxy_entry_still_matches() {
        // We only ever request 443, so honouring the port could only ever reject
        // a `:443` entry the user plainly meant to apply. `:8080` is the same
        // entry with a port that cannot arise here — still a bypass, not a trap.
        assert_eq!(with_no_proxy("api.github.com:443"), None);
        assert_eq!(with_no_proxy("github.com:8080"), None);
    }

    #[test]
    fn no_proxy_splits_on_commas_and_tolerates_whitespace_and_blanks() {
        // A hand-written list is rarely tight: `NO_PROXY="localhost, .github.com,"`.
        assert_eq!(with_no_proxy("localhost, .github.com ,"), None);
        // …and an entry list that misses must still leave the proxy in place.
        assert_eq!(
            with_no_proxy("localhost, 127.0.0.1").as_deref(),
            Some("http://corp:8080")
        );
    }

    #[test]
    fn no_proxy_matching_ignores_case_on_both_sides() {
        assert_eq!(with_no_proxy("API.GitHub.COM"), None);
        assert_eq!(
            select_https_proxy(
                "https://API.GITHUB.COM/repos",
                env(&[
                    ("HTTPS_PROXY", "http://corp:8080"),
                    ("no_proxy", "api.github.com"),
                ]),
            ),
            None
        );
    }

    #[test]
    fn no_proxy_only_matches_on_a_label_boundary() {
        // The classic bug: a bare suffix test lets `hub.com` — a different
        // registrable domain someone happens to have listed — silently disable
        // the proxy for github.com, so the request goes direct and fails on a
        // host that has no direct route at all.
        assert_eq!(
            with_no_proxy("hub.com").as_deref(),
            Some("http://corp:8080")
        );
        assert_eq!(
            with_no_proxy("notgithub.com").as_deref(),
            Some("http://corp:8080")
        );
        // Reverse direction: a *sub*domain entry must not exempt its parent.
        assert_eq!(
            select_https_proxy(
                "https://github.com/x",
                env(&[
                    ("HTTPS_PROXY", "http://corp:8080"),
                    ("NO_PROXY", "api.github.com"),
                ]),
            )
            .as_deref(),
            Some("http://corp:8080")
        );
    }

    #[test]
    fn an_empty_or_absent_no_proxy_leaves_the_proxy_alone() {
        // Same "empty means unset" rule as the proxy vars themselves — and a
        // list of nothing but separators must not read as a wildcard.
        assert_eq!(with_no_proxy("").as_deref(), Some("http://corp:8080"));
        assert_eq!(with_no_proxy("  ").as_deref(), Some("http://corp:8080"));
        assert_eq!(with_no_proxy(" , , ").as_deref(), Some("http://corp:8080"));
    }

    #[test]
    fn no_proxy_cannot_conjure_a_proxy_that_was_never_set() {
        // Order matters: the bypass list is only consulted once a proxy exists,
        // so a host with only NO_PROXY set stays direct rather than erroring.
        assert_eq!(select_https_proxy(TARGET, env(&[("NO_PROXY", "*")])), None);
    }

    #[test]
    fn the_host_is_taken_from_the_url_without_scheme_port_path_or_userinfo() {
        // Each of these would otherwise leak into the comparison and make an
        // exact-match entry silently miss.
        assert_eq!(host_of(TARGET), "api.github.com");
        assert_eq!(
            host_of("https://user:pw@API.github.com:8443/a?b#c"),
            "api.github.com"
        );
        assert_eq!(host_of("http://localhost:3000"), "localhost");
        // A fully-qualified name's root dot is not part of the label.
        assert_eq!(host_of("https://api.github.com./x"), "api.github.com");
    }
}
