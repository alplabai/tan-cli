// SPDX-License-Identifier: Apache-2.0
//! Records which SDK root + precedence tier a resolver in [`crate::util`]
//! ACTUALLY used, so `Envelope::new` can report it without a second
//! resolution.
//!
//! `util::resolve_sdk_root` and `util::resolve_cli_project_context` walk
//! DIFFERENT candidate sets (`resolve_sdk_root`'s auto-discovery, for one,
//! also probes a sibling `alp-sdk-upstream` the other never considers), and
//! each calls [`record`] at its own return, from the branch it actually took.
//! `util::resolve_sdk_tiered` deliberately does NOT record itself — one of
//! its two call sites (`sdk.rs`'s `switch_cache_roots`) only wants the
//! CURRENT active SDK as a candidate cache root for a `tan sdk switch` that
//! is about to repoint the pin to something else, so recording there would
//! report the SDK a switch just replaced, not the one it switched to. The
//! call site that treats the result as "the active SDK" (`sdk.rs`'s
//! `run_current`) records it explicitly.
//!
//! [`record`] normalizes `root` to forward slashes before storing — matching
//! `tan_core::project::to_posix`'s "platform-identical" rule for every field
//! in the extension/CLI handshake — so `sdk.root` never diverges by
//! separator style depending on which resolver happened to record it.

use tan_core::SdkSourceTier;

thread_local! {
    // ponytail: thread-local + first-writer-wins is the whole design. tan
    // resolves exactly one project per process today (one command line, one
    // resolution), so this only ever needs to survive across a single
    // command's own resolver calls — a process-wide `Mutex<Option<..>>`
    // would only buy contention between `#[test]` threads that share nothing
    // else the recorder cares about, while still needing the same
    // first-writer-wins logic. If tan ever resolves more than one project
    // per process (a hypothetical multi-project daemon mode), stop reaching
    // for a global at all — thread the resolved `(path, tier)` through an
    // explicit per-invocation context struct instead.
    static RESOLVED: std::cell::RefCell<Option<(String, SdkSourceTier)>> =
        const { std::cell::RefCell::new(None) };
}

/// Record the SDK root + tier a resolver's own branch actually produced.
/// First writer wins (per-thread): a single command that calls two resolvers
/// (`tan size`/`flash`/`run`/`clean` each build a `ProjectContext` and then
/// separately call `resolve_sdk_root`) keeps whichever recorded first — the
/// one the project context was actually built from — rather than whichever
/// resolver happened to run last.
///
/// `root` is normalized to forward slashes before storing: callers pass
/// through whatever separator style their own path came in with (a raw
/// `--sdk-root` argument, a native `PathBuf::to_string_lossy()`, an
/// already-`to_posix`'d core value, …), and the emitted `sdk.root` must be
/// platform-identical regardless of which one recorded it — the same rule
/// `tan_core::project::to_posix` enforces for `project.root`.
pub fn record(root: &str, tier: SdkSourceTier) {
    RESOLVED.with(|cell| {
        let mut slot = cell.borrow_mut();
        if slot.is_none() {
            *slot = Some((root.replace('\\', "/"), tier));
        }
    });
}

/// Take (and clear) this thread's recorded value. `Envelope::new` calls this
/// exactly once per envelope, so a second, unrelated `Envelope::new` built on
/// the same thread right after (routine in `#[cfg(test)]` unit tests, which
/// all run their bodies on one thread per test but can construct many
/// envelopes in a row) never inherits a value a previous call already
/// consumed and reported.
pub fn take() -> Option<(String, SdkSourceTier)> {
    RESOLVED.with(|cell| cell.borrow_mut().take())
}

/// Clear this thread's recorded value without reading it. Any test that
/// builds an `Envelope` without first driving a real resolver call — or that
/// asserts the recorder is empty — must call this first: `take()` only
/// clears on read, so a value a *different* test left behind on a reused
/// thread would otherwise leak in. `#[cfg(test)]`-only: production code never
/// needs to clear a recording it hasn't consumed.
#[cfg(test)]
pub fn reset() {
    RESOLVED.with(|cell| *cell.borrow_mut() = None);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_writer_wins() {
        reset();
        record("/first", SdkSourceTier::SdkRootFlag);
        record("/second", SdkSourceTier::Discovery);
        assert_eq!(
            take(),
            Some(("/first".to_string(), SdkSourceTier::SdkRootFlag))
        );
    }

    #[test]
    fn take_clears_so_a_later_read_on_the_same_thread_sees_nothing() {
        reset();
        record("/only", SdkSourceTier::ProjectPin);
        assert!(take().is_some());
        assert_eq!(take(), None);
    }

    #[test]
    fn reset_clears_without_needing_a_read() {
        record("/leftover", SdkSourceTier::GlobalDefault);
        reset();
        assert_eq!(take(), None);
    }
}
