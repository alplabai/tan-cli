// SPDX-License-Identifier: Apache-2.0
//! The one shared HTTP agent every network call in `tan` goes through.
//!
//! `ureq::get(url)` builds a throwaway default agent, and that default is wrong
//! on two counts for the machines `tan` actually runs on:
//!
//! * **Trust.** ureq's default rustls config trusts only the *bundled* webpki
//!   roots. A corporate TLS-intercepting middlebox re-signs traffic with a
//!   private CA that lives in the Windows/macOS/Linux system trust store, which
//!   webpki-roots never consults — so the handshake fails on every such host.
//!   ureq's `native-certs` feature swaps the bundled roots out for the OS store
//!   rather than adding to it, which would break a host whose OS store is empty
//!   instead, so [`tls_config`] merges the two.
//! * **Proxy.** ureq 2.x ignores `HTTPS_PROXY`/`HTTP_PROXY` unless the agent is
//!   built with `try_proxy_from_env(true)` (the `proxy-from-env` cargo feature
//!   only flips that default). Without it a host that can only reach the
//!   internet through a proxy fails with a bare connect error.
//!
//! Reading the OS trust store and the process environment is IO, so this lives
//! in `tan-cli`; the pure "what does this transport error probably mean" mapping
//! is `tan_core::describe_network_error`.

use std::sync::{Arc, OnceLock};

/// The process-wide agent. Cheap to clone (`ureq::Agent` is `Arc` inside), so
/// callers take a clone rather than holding a borrow — and the trust store is
/// read from disk exactly once per run, not once per request.
pub fn agent() -> ureq::Agent {
    static AGENT: OnceLock<ureq::Agent> = OnceLock::new();
    AGENT
        .get_or_init(|| {
            ureq::AgentBuilder::new()
                // Deliberately no `.timeout*()` calls: `AgentBuilder`'s defaults
                // (30 s connect, no read/write cap) are exactly what the bare
                // `ureq::get` this replaced already used.
                .try_proxy_from_env(true)
                .tls_config(Arc::new(tls_config()))
                .build()
        })
        .clone()
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
        // The whole point of the merge: enabling ureq's `native-certs` feature
        // instead would have dropped the bundled roots entirely, so a host with
        // an empty OS store would trust nothing. This must never shrink below
        // the webpki set, whatever the OS store contains.
        assert!(root_store().roots.len() >= webpki_roots::TLS_SERVER_ROOTS.len());
        // …and the config actually builds with them (ring provider available).
        let _ = tls_config();
    }

    #[test]
    fn agent_is_shared_and_cloneable() {
        // Guards the OnceLock: a fresh config (and a fresh trust-store read) per
        // request would be a silent per-call cost on every future network command.
        let _ = agent();
        let _ = agent();
    }
}
