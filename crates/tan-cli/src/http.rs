// SPDX-License-Identifier: Apache-2.0
//! The one shared HTTP agent every *in-process* network call in `tan` goes
//! through. Subprocesses `tan` spawns (`git clone`, `pip`, `west update`) do
//! their own networking with their own trust stores — they inherit the proxy
//! environment but nothing here applies to them.
//!
//! `ureq::get(url)` builds a throwaway default agent, and that default is wrong
//! on three counts for the machines `tan` actually runs on:
//!
//! * **Trust.** ureq's default rustls config trusts only the *bundled* webpki
//!   roots. A corporate TLS-intercepting middlebox re-signs traffic with a
//!   private CA that lives in the Windows/macOS/Linux system trust store, which
//!   webpki-roots never consults — so the handshake fails on every such host.
//!   ureq's `native-certs` feature swaps the bundled roots out for the OS store
//!   rather than adding to it, which would break a host whose OS store is empty
//!   instead, so [`tls_config`] merges the two.
//! * **Proxy.** ureq 2.x ignores `ALL_PROXY`/`HTTPS_PROXY`/`HTTP_PROXY` unless
//!   the agent is built with `try_proxy_from_env(true)` (the `proxy-from-env`
//!   cargo feature only flips that default). Without it a host that can only
//!   reach the internet through a proxy fails with a bare connect error.
//! * **Timeout.** the default agent caps the *connect* at 30 s and nothing else.
//!   Now that the request can be routed through a proxy, a black-hole proxy that
//!   completes the TCP connect and then never answers would hang `tan` forever —
//!   and `alp-sdk-vscode` shells this binary and waits on process exit. Hence the
//!   total cap below.
//!
//! **Known limitation: `NO_PROXY` is not honoured.** ureq 2.12 has no support
//! for it at all, and the only host this module ever talks to is GitHub's API —
//! an external host, which is not what a `NO_PROXY` list is for. Implementing it
//! here would mean building the agent per-URL instead of once per process; if a
//! second, possibly-internal endpoint ever appears, do that then.
//!
//! Reading the OS trust store and the process environment is IO, so this lives
//! in `tan-cli`; the pure "what does this transport error probably mean" mapping
//! is `tan_core::describe_network_error`.

use std::sync::{Arc, OnceLock};
use std::time::Duration;

/// The process-wide agent: built once, so the OS trust store is read from disk
/// exactly once per run rather than once per request. Returned by reference
/// (rather than cloned) so that the sharing is observable — see the test.
pub fn agent() -> &'static ureq::Agent {
    static AGENT: OnceLock<ureq::Agent> = OnceLock::new();
    AGENT.get_or_init(|| {
        ureq::AgentBuilder::new()
            // Total request cap, not just the 30 s connect the default gives us.
            // Every call through here is a single small GitHub API GET; a minute
            // is generous for that and short enough that a wedged proxy surfaces
            // as an error the user can read instead of a hung process.
            .timeout(Duration::from_secs(60))
            .try_proxy_from_env(true)
            .tls_config(Arc::new(tls_config()))
            .build()
    })
}

/// Environment variables ureq's `try_proxy_from_env` reads, in its own order
/// (`ALL_PROXY` first — see ureq `proxy.rs`).
const PROXY_ENV_VARS: [&str; 6] = [
    "ALL_PROXY",
    "all_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
];

/// Whether this process is configured to reach the network through a proxy.
///
/// The one bit `tan_core::describe_network_error` cannot see for itself: a proxy
/// that is unreachable, refused or firewalled fails as a plain `ConnectionFailed`
/// that never says the word "proxy", so the raw string alone cannot tell that
/// case apart from a genuinely dead network. Subprocesses inherit these too,
/// which is why `git clone` failures get the same treatment.
pub fn proxy_configured() -> bool {
    PROXY_ENV_VARS
        .iter()
        .any(|k| std::env::var_os(k).is_some_and(|v| !v.is_empty()))
}

/// rustls config trusting the bundled webpki roots **and** the OS trust store.
///
/// Mirrors ureq's own `default_tls_config` (ring provider, TLS 1.2 + 1.3) and
/// only widens the root store.
fn tls_config() -> rustls::ClientConfig {
    rustls::ClientConfig::builder_with_provider(rustls::crypto::ring::default_provider().into())
        .with_protocol_versions(&[&rustls::version::TLS12, &rustls::version::TLS13])
        .expect("ring provider supports TLS 1.2 and 1.3")
        .with_root_certificates(root_store())
        .with_no_client_auth()
}

/// The merged trust anchors. A system store that fails to load is not fatal —
/// the webpki roots still stand, which is the pre-fix behaviour.
fn root_store() -> rustls::RootCertStore {
    let mut roots = rustls::RootCertStore {
        roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
    };
    let native = rustls_native_certs::load_native_certs().unwrap_or_default();
    roots.add_parsable_certificates(native);
    roots
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn root_store_keeps_the_bundled_roots_on_top_of_the_os_store() {
        // Presence, not count: `add_parsable_certificates` only ever appends, so
        // a length comparison holds by construction and would catch nothing. The
        // regression worth guarding is someone switching to ureq's `native-certs`
        // feature, which REPLACES the bundled set — on a slim container with no
        // ca-certificates package that host would then trust nothing at all.
        let merged = root_store();
        for anchor in webpki_roots::TLS_SERVER_ROOTS {
            assert!(
                merged
                    .roots
                    .iter()
                    .any(|r| r.subject.as_ref() == anchor.subject.as_ref()),
                "bundled webpki anchor missing from the merged store"
            );
        }
        // …and the config actually builds with them (ring provider available).
        let _ = tls_config();
    }

    #[test]
    fn agent_is_built_once_per_process() {
        // Pointer identity is the observable form of the guarantee: rebuild the
        // agent per call and this fails, which is exactly the regression (a fresh
        // OS-trust-store read on every request) the OnceLock exists to prevent.
        assert!(std::ptr::eq(agent(), agent()));
    }

    #[test]
    fn socks_proxy_support_is_compiled_in() {
        // ureq reads ALL_PROXY *first*, so a dev box or WSL SSH tunnel exporting
        // `ALL_PROXY=socks5://…` routes this agent through SOCKS. Without ureq's
        // `socks-proxy` feature its connector is a stub that fails every such
        // request with "SOCKS feature disabled." — a host that worked before we
        // read the proxy env at all would regress to hard-broken.
        //
        // Port 1 refuses instantly, so this never touches the network: all we
        // assert is WHICH error comes back.
        let agent = ureq::AgentBuilder::new()
            .proxy(ureq::Proxy::new("socks5://127.0.0.1:1").expect("socks5 proxy URL parses"))
            .build();
        let err = agent
            .get("http://example.invalid/")
            .call()
            .expect_err("nothing is listening on port 1")
            .to_string();
        assert!(!err.contains("SOCKS feature disabled"), "{err}");
    }
}
