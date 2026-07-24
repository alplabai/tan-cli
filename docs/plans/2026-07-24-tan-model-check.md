# `tan model check` — Envelope Wrapper for the Static Analyzer (Slice 1b)

**Goal:** Add `tan model check <model> --sku <SKU>` — a thin envelope wrapper over alp-sdk's `alp model check` (slice 1a), mirroring `tan model list`/`info`.

**Architecture:** `ModelSub::Check(ModelCheckArgs)` — a new wrappable subcommand. `sub_argv` maps it to the `alp_cli model check <model> --sku <SKU> [--metadata-root …]` argv tail; `model_argv` appends `--format json`; the existing `wrap_model_json` wraps the `{model,sku,backends,suggestion}` payload into tan's `{command,ok,exitCode,project,data,issues}` envelope. On failure, `wrap_model_json` already captures the child's stderr into a `model.failed` issue — no new error handling needed.

**Tech Stack:** Rust (edition 2024, rustc ≥1.85), clap 4.6. The four cargo gates: `cargo fmt --all --check`, `cargo clippy --all-targets -- -D warnings`, `cargo build --all-targets`, `cargo test`.

## Global Constraints

- **Thin passthrough only** — no fit/analysis logic in tan; it forwards to `alp model check` and wraps the envelope. Mirror the existing `Info` arm exactly.
- **`Check` is wrappable** (emits `--format json`) — `is_wrappable` already returns true for everything except `Run`, so NO change there.
- **No 1a change** — the alp-sdk `check_cmd` (stderr `error: …` on failure) surfaces through `wrap_model_json`'s `model.failed` issue as-is.
- **Branch:** `feat/tan-model-check`, stacked on `feat/tan-model-envelope` (#47). PR targets `dev` (retarget when #47 lands). Do NOT merge. NO Claude/AI attribution.
- **This is tan-cli** — work→dev→main via PR, `enforce_admins=true`; never push to `dev`/`main` directly.

---

## Task 1: `ModelSub::Check` variant + `ModelCheckArgs`

**Files:**
- Modify: `crates/tan-cli/src/cli.rs`

**Interfaces:**
- Produces: `ModelSub::Check(ModelCheckArgs)` where `ModelCheckArgs { model: String (positional), sku: String (--sku, required), metadata_root: Option<String> (--metadata-root) }`.

- [ ] **Step 1: Add the enum variant**

In `crates/tan-cli/src/cli.rs`, in `enum ModelSub` (after `Info(ModelInfoArgs)`, before `Doctor`), add:

```rust
    /// Static pre-flight fit/perf check for a model on a SoM (offline, no toolchain).
    Check(ModelCheckArgs),
```

- [ ] **Step 2: Add the args struct**

Immediately after the `ModelInfoArgs` struct definition, add (mirrors `ModelInfoArgs`, but `--sku` required + a positional model path):

```rust
/// Args for `model check`.
#[derive(Debug, Args)]
pub struct ModelCheckArgs {
    /// Model file to check (`.tflite`).
    #[arg(value_name = "MODEL")]
    pub model: String,
    /// SoM SKU, e.g. `E1M-AEN801`.
    #[arg(long, value_name = "SKU")]
    pub sku: String,
    /// Path to the metadata/ root (default: the SDK's own `metadata/`).
    #[arg(long = "metadata-root")]
    pub metadata_root: Option<String>,
}
```

- [ ] **Step 3: Build to confirm it compiles (exhaustiveness will fail in model.rs — expected)**

Run: `cd /e/GitHub/tan-cli/.claude/worktrees/model-check && cargo build --all-targets 2>&1 | head -20`
Expected: a non-exhaustive-match error in `commands/model.rs` `sub_argv` (the new variant isn't handled yet) — that is Task 2.

---

## Task 2: `sub_argv` arm + test

**Files:**
- Modify: `crates/tan-cli/src/commands/model.rs`

**Interfaces:**
- Consumes: `ModelSub::Check` + `ModelCheckArgs` (Task 1).
- Produces: `sub_argv(Check)` → `["check", <model>, "--sku", <sku>, ("--metadata-root", <path>)?]`.

- [ ] **Step 1: Add the `sub_argv` arm**

In `crates/tan-cli/src/commands/model.rs` `fn sub_argv`, add an arm after the `Info` arm (before `Doctor`):

```rust
        ModelSub::Check(a) => {
            let mut argv = vec!["check".to_string(), a.model.clone()];
            argv.push("--sku".to_string());
            argv.push(a.sku.clone());
            push_opt(&mut argv, "--metadata-root", &a.metadata_root);
            argv
        }
```

- [ ] **Step 2: Add the import to the test module + a mapping test**

In the `#[cfg(test)] mod tests` block, extend the `use crate::cli::{…}` import to include `ModelCheckArgs`, then add:

```rust
    #[test]
    fn sub_argv_maps_check_with_required_sku() {
        let sub = ModelSub::Check(ModelCheckArgs {
            model: "m.tflite".to_string(),
            sku: "E1M-AEN801".to_string(),
            metadata_root: None,
        });
        assert_eq!(sub_argv(&sub), vec!["check", "m.tflite", "--sku", "E1M-AEN801"]);
    }

    #[test]
    fn model_argv_appends_format_json_for_check() {
        let sub = ModelSub::Check(ModelCheckArgs {
            model: "m.tflite".to_string(),
            sku: "E1M-AEN801".to_string(),
            metadata_root: Some("meta".to_string()),
        });
        let argv = model_argv(&sub, true);
        // -m alp_cli model check m.tflite --sku E1M-AEN801 --metadata-root meta --format json
        assert!(argv.ends_with(&["--format".to_string(), "json".to_string()]));
        assert!(argv.contains(&"check".to_string()));
        assert!(argv.contains(&"--metadata-root".to_string()));
        assert!(is_wrappable(&sub), "check must be wrappable");
    }
```

(Confirm `model_argv`/`is_wrappable` are already imported in the test module — the existing `sub_argv_*`/`model_argv_*` tests use them. If a symbol isn't in scope, add it to the test module's `use super::*;`/`use crate::…` line, matching the existing tests.)

- [ ] **Step 3: The four cargo gates (all must pass)**

Run in `/e/GitHub/tan-cli/.claude/worktrees/model-check`:
```bash
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
cargo build --all-targets
cargo test
```
Expected: all green; the two new tests pass.

- [ ] **Step 4: Commit**

```bash
git add crates/tan-cli/src/cli.rs crates/tan-cli/src/commands/model.rs
git commit -m "feat(model): add 'tan model check' envelope wrapper"
```

---

## Self-Review

- **Spec coverage:** roadmap §3.5 "tan-cli: `tan model check` — envelope-wraps it (same pattern as `tan model {build,list,info,doctor}`)" → Tasks 1+2. ✓
- **Placeholder scan:** all code complete; the only judgment call is confirming the test-module `use` line (Task 2 Step 2) — explicitly instructed to match existing tests.
- **Type consistency:** `ModelCheckArgs` fields (`model`/`sku`/`metadata_root`) identical across cli.rs, sub_argv, and both tests; `sub_argv(Check)` output shape matches what `model_argv` + `wrap_model_json` consume (they're generic over the argv/payload — no per-command coupling).
