// SPDX-License-Identifier: Apache-2.0
//! Which proxy environment variable applies to an `https://` request.
//!
//! Pure on purpose. Reading the environment is IO and lives in `tan-cli`
//! (`http::env_proxy_url`); the *precedence rule* is a decision, and a decision
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

/// Proxy env vars that apply to an `https://` URL, in the precedence order ureq
/// itself uses (`ALL_PROXY` first, lowercase alias immediately after its
/// uppercase form). `HTTP_PROXY`/`http_proxy` are deliberately absent — see the
/// module docs.
pub const HTTPS_PROXY_ENV_VARS: [&str; 4] =
    ["ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"];

/// The proxy URL an `https://` request should go through, or `None` for direct.
///
/// `lookup` is the injected environment read. An empty value counts as unset:
/// exporting `HTTPS_PROXY=` is the conventional way to *disable* an inherited
/// proxy, and treating it as a proxy URL would make every request fail.
pub fn select_https_proxy(lookup: impl Fn(&str) -> Option<String>) -> Option<String> {
    HTTPS_PROXY_ENV_VARS
        .iter()
        .find_map(|key| lookup(key).filter(|value| !value.trim().is_empty()))
}

#[cfg(test)]
mod tests {
    use super::*;

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

    #[test]
    fn all_proxy_outranks_https_proxy() {
        let chosen = select_https_proxy(env(&[
            ("HTTPS_PROXY", "http://corp:8080"),
            ("ALL_PROXY", "socks5://127.0.0.1:1080"),
        ]));
        assert_eq!(chosen.as_deref(), Some("socks5://127.0.0.1:1080"));
    }

    #[test]
    fn uppercase_outranks_its_lowercase_alias() {
        let chosen = select_https_proxy(env(&[
            ("https_proxy", "http://lower:8080"),
            ("HTTPS_PROXY", "http://upper:8080"),
        ]));
        assert_eq!(chosen.as_deref(), Some("http://upper:8080"));
    }

    #[test]
    fn http_proxy_alone_leaves_an_https_request_direct() {
        // The regression this whole module exists to prevent: ureq's own env
        // reader would proxy through this, and a plain-HTTP-only proxy refusing
        // CONNECT then breaks a host that worked.
        assert_eq!(
            select_https_proxy(env(&[
                ("HTTP_PROXY", "http://corp:8080"),
                ("http_proxy", "http://corp:8080"),
            ])),
            None
        );
    }

    #[test]
    fn an_empty_value_means_unset_and_falls_through() {
        // `HTTPS_PROXY=` is how a shell disables an inherited proxy; it must not
        // shadow a real `ALL_PROXY`, and on its own it must not be a proxy URL.
        assert_eq!(
            select_https_proxy(env(&[
                ("ALL_PROXY", "   "),
                ("HTTPS_PROXY", "http://corp:8080")
            ]))
            .as_deref(),
            Some("http://corp:8080")
        );
        assert_eq!(select_https_proxy(env(&[("HTTPS_PROXY", "")])), None);
    }

    #[test]
    fn nothing_set_is_direct() {
        assert_eq!(select_https_proxy(env(&[])), None);
    }
}
