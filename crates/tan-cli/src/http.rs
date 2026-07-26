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
//! * **Proxy.** ureq 2.x ignores `ALL_PROXY`/`HTTPS_PROXY` unless told to read
//!   them (`try_proxy_from_env`, which the `proxy-from-env` cargo feature only
//!   flips the default of). Without it a host that can only reach the internet
//!   through a proxy fails with a bare connect error. We read the environment
//!   ourselves rather than using `try_proxy_from_env(true)` — see below.
//! * **Timeout.** the default agent caps the *connect* at 30 s and nothing else.
//!   Now that the request can be routed through a proxy, a black-hole proxy that
//!   completes the TCP connect and then never answers would hang `tan` forever —
//!   and `alp-sdk-vscode` shells this binary and waits on process exit. Hence the
//!   total cap below.
//!
//! **Why we pick the proxy ourselves.** ureq 2.12's `try_proxy_from_env` also
//! reads `HTTP_PROXY`/`http_proxy` and applies them to an `https://` URL, which
//! curl, git and Python all treat as http-only. A corporate box that exports
//! only `HTTP_PROXY` would then push its GitHub request through a proxy that may
//! refuse `CONNECT`, breaking a machine that previously worked going direct. The
//! precedence rule is pure and lives in `tan_core::select_https_proxy`; reading
//! the environment is the IO part and stays here.
//!
//! `NO_PROXY` is honoured for the same reason. ureq 2.12 has no support for it,
//! and without it a corporate host that sets both `HTTPS_PROXY` and a `NO_PROXY`
//! covering GitHub would go from working-direct to proxied — the same
//! broke-a-working-machine failure the `HTTP_PROXY` rule avoids, aimed at the
//! population this module exists to help. The matching rules live with the
//! precedence rule in `tan_core::select_https_proxy`. Applying them costs
//! nothing here because `GITHUB_RELEASES_URL` is the only URL any in-process
//! call requests, so the one process-wide agent has exactly one target host; a
//! second endpoint would need the agent built per-host instead.
//!
//! Reading the OS trust store and the process environment is IO, so this lives
//! in `tan-cli`; the pure "what does this transport error probably mean" mapping
//! is `tan_core::describe_network_error`.

use std::sync::{Arc, OnceLock};
use std::time::Duration;

/// Total request cap, not just the 30 s connect the default gives us. Every call
/// through here is a single small GitHub API GET; a minute is generous for that
/// and short enough that a wedged proxy surfaces as an error the user can read
/// instead of a hung process.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(60);

/// The process-wide agent: built once, so the OS trust store is read from disk
/// exactly once per run rather than once per request. Returned by reference
/// (rather than cloned) so that the sharing is observable — see the test.
pub fn agent() -> &'static ureq::Agent {
    static AGENT: OnceLock<ureq::Agent> = OnceLock::new();
    AGENT.get_or_init(|| build_agent(REQUEST_TIMEOUT, env_proxy()))
}

/// Assemble the agent from its two configurable parts.
///
/// Split out from [`agent`] purely so both of those parts are testable: the
/// 60 s production cap is far too long to drive from a test, and the proxy comes
/// from the process environment, which a test must not mutate. With them as
/// arguments a test can point a 1 s agent at a local socket instead — which
/// `commands::sdk`'s `fetch_releases` test also does, hence `pub(crate)`.
pub(crate) fn build_agent(timeout: Duration, proxy: Option<ureq::Proxy>) -> ureq::Agent {
    let mut builder = ureq::AgentBuilder::new()
        .timeout(timeout)
        .tls_config(Arc::new(tls_config()));
    if let Some(proxy) = proxy {
        builder = builder.proxy(proxy);
    }
    builder.build()
}

/// The proxy this process should route its requests through, parsed, or `None`
/// for a direct connection.
///
/// Reading the environment is the only IO here; which variable wins, and whether
/// `NO_PROXY` exempts the target, are decided by the pure rule in `tan-core`.
/// Called with [`tan_core::GITHUB_RELEASES_URL`] because that is the only URL
/// any in-process request targets — see the module docs.
///
/// An unparseable value degrades to a direct connection rather than failing the
/// command: `ureq::Proxy::new` rejects e.g. a `pac+http://` URL, and going
/// direct is what happened before we read the environment at all.
fn env_proxy() -> Option<ureq::Proxy> {
    let url =
        tan_core::select_https_proxy(tan_core::GITHUB_RELEASES_URL, |key| std::env::var(key).ok())?;
    ureq::Proxy::new(url).ok()
}

/// Whether this process will actually reach the network through a proxy.
///
/// The one bit `tan_core::describe_network_error` cannot see for itself: a proxy
/// that is unreachable, refused or firewalled fails as a plain `ConnectionFailed`
/// that never says the word "proxy", so the raw string alone cannot tell that
/// case apart from a genuinely dead network. Deliberately the *same* answer
/// [`env_proxy`] gives rather than a second reading of the environment: a
/// scheme-only or `NO_PROXY`-exempt or unparseable value means the request went
/// direct, and telling the user to go check their proxy settings then sends them
/// to a knob that had no effect on the failure they are looking at. The one
/// approximation left: `sdk install`'s `git clone` targets `github.com`, not
/// `api.github.com`, so a `NO_PROXY` naming only the API host would make this
/// under-report for that caller — a missing hint, never a wrong one.
pub fn proxy_configured() -> bool {
    env_proxy().is_some()
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
    use std::net::TcpListener;
    use std::sync::mpsc;

    /// A bound-but-never-accepted listener. The kernel completes the TCP
    /// handshake from the backlog, so a connection to it *establishes* and then
    /// waits forever for bytes that never come — a black-hole proxy in one line,
    /// with no network access and no second thread to join.
    fn black_hole() -> (TcpListener, String) {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind a loopback port");
        let addr = listener
            .local_addr()
            .expect("the bound address")
            .to_string();
        (listener, addr)
    }

    /// Run one request on a worker thread and give up after `patience`.
    ///
    /// Deliberately not a plain call: if the agent's timeout is ever dropped,
    /// the request against `black_hole()` hangs forever, and a hanging test is
    /// indistinguishable from a wedged CI runner. This turns that mutation into
    /// a named assertion failure a few seconds later.
    fn call_with_patience(
        agent: ureq::Agent,
        url: String,
        patience: Duration,
    ) -> Result<ureq::Error, mpsc::RecvTimeoutError> {
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let outcome = agent.get(&url).call().err();
            let _ = tx.send(outcome);
        });
        rx.recv_timeout(patience)
            .map(|outcome| outcome.expect("a black hole never answers, so this cannot succeed"))
    }

    /// Covers [`root_store`] only — **not** the CA fix end to end.
    ///
    /// Worth stating plainly, because "the CA merge is tested" is the easy
    /// misreading: this proves the merged store contains both sets of anchors,
    /// and nothing here proves [`build_agent`] installs that store on the agent.
    /// Delete `.tls_config(…)` from `build_agent` and the agent silently reverts
    /// to bundled-roots-only — the whole fix undone — with this test still green.
    /// Closing that gap needs a TLS server with a throwaway CA (rcgen + a rustls
    /// listener), which is a dependency and a fixture out of proportion to a
    /// three-line builder call; if this module ever grows a second TLS concern,
    /// build it then.
    #[test]
    fn the_os_trust_store_is_merged_in_on_top_of_the_bundled_roots() {
        let merged = root_store();
        // Direction 1 — the bundled roots survive. Guards someone switching to
        // ureq's `native-certs` feature, which REPLACES them: on a slim
        // container with no ca-certificates package that host would then trust
        // nothing at all.
        for anchor in webpki_roots::TLS_SERVER_ROOTS {
            assert!(
                merged
                    .roots
                    .iter()
                    .any(|r| r.subject.as_ref() == anchor.subject.as_ref()),
                "bundled webpki anchor missing from the merged store"
            );
        }
        // Direction 2 — the OS store actually got added. This is the whole point
        // of the module (a corporate CA lives only there), and it is the half
        // that `add_parsable_certificates` does NOT make true by construction:
        // drop the two lines in `root_store` and only this assertion notices.
        // Gated on the host having any system certs at all, so a bare container
        // stays green rather than failing for a reason that is not a regression.
        let native = rustls_native_certs::load_native_certs().unwrap_or_default();
        if !native.is_empty() {
            assert!(
                merged.roots.len() > webpki_roots::TLS_SERVER_ROOTS.len(),
                "{} OS certificates were available but none reached the merged store",
                native.len()
            );
        }
        // …and the config actually builds with them (ring provider available).
        // Note this is still only `tls_config`, not the agent — see above.
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
    fn the_timeout_bounds_a_request_the_peer_never_answers() {
        let (_listener, addr) = black_hole();
        let err = call_with_patience(
            build_agent(Duration::from_secs(1), None),
            format!("http://{addr}/"),
            Duration::from_secs(20),
        )
        .expect("the 1 s cap must fire long before the 20 s patience");
        // ureq surfaces the expired total timeout as an IO error; what matters
        // is that it came back at all, and that it is not something else (a
        // refused connect would mean the black hole was not actually reached).
        assert_eq!(err.kind(), ureq::ErrorKind::Io, "{err}");
    }

    #[test]
    fn the_agent_routes_through_the_proxy_it_is_given() {
        let (_listener, addr) = black_hole();
        // `example.invalid` is guaranteed by RFC 6761 never to resolve, so a
        // direct attempt dies instantly in DNS. Reaching the black hole — and
        // therefore hitting the timeout instead — is only possible if the proxy
        // is honoured: drop `.proxy()` from `build_agent` and this comes back
        // `ErrorKind::Dns`.
        let err = call_with_patience(
            build_agent(
                Duration::from_secs(1),
                Some(ureq::Proxy::new(format!("http://{addr}")).expect("proxy URL parses")),
            ),
            "http://example.invalid/".to_string(),
            Duration::from_secs(20),
        )
        .expect("the 1 s cap must fire long before the 20 s patience");
        assert_ne!(
            err.kind(),
            ureq::ErrorKind::Dns,
            "the request resolved the target host itself, so the proxy was ignored: {err}"
        );
        assert_eq!(err.kind(), ureq::ErrorKind::Io, "{err}");
    }

    #[test]
    fn socks_proxy_support_is_compiled_in() {
        // ureq reads ALL_PROXY *first*, so a dev box or WSL SSH tunnel exporting
        // `ALL_PROXY=socks5://…` routes this agent through SOCKS. Without ureq's
        // `socks-proxy` feature its connector is a stub that fails every such
        // request with "SOCKS feature disabled." — a host that worked before we
        // read the proxy env at all would regress to hard-broken.
        //
        // Port 1 refuses instantly on any normal host, so this never touches the
        // network; the 5 s cap is there for the abnormal one, where a DROP-style
        // local firewall would otherwise wedge `cargo test` indefinitely.
        let err = call_with_patience(
            build_agent(
                Duration::from_secs(5),
                Some(ureq::Proxy::new("socks5://127.0.0.1:1").expect("socks5 proxy URL parses")),
            ),
            "http://example.invalid/".to_string(),
            Duration::from_secs(30),
        )
        .expect("nothing is listening on port 1, and the 5 s cap covers a firewall that drops")
        .to_string();
        assert!(!err.contains("SOCKS feature disabled"), "{err}");
    }
}
