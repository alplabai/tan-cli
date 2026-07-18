// SPDX-License-Identifier: Apache-2.0
//! `tan` — the standalone Alp Lab build CLI.
//!
//! Consumes the alp-sdk build-plan (`python -m alp_orchestrate --emit
//! build-plan`) and executes it. Under alp-sdk ADR-0020 (end-state B) `tan`
//! is the *sole executor* and the *whole* user command surface; alp-sdk is
//! a plans-only backend and alp-sdk-vscode is a thin extension that shells
//! this binary. Dependency direction is one-way: extension -> tan -> alp-sdk.
//!
//! DRAFT SKELETON — subcommands are stubs. Real implementations land in
//! Phase 2, reconciled with alp-sdk-vscode's `cli-rs`. See README.md.

mod build_plan;
mod commands;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "tan", version, about = "Alp Lab build CLI (draft skeleton)")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Build the project (plan -> materialise -> execute per slice).
    Build(commands::BuildArgs),
    /// Flash a built slice to the target.
    Flash,
    /// Package a build into a deployable image.
    Image,
    /// Report per-slice binary sizes.
    Size,
    /// Run a slice under Renode.
    Renode,
    /// Remove build artefacts.
    Clean,
    /// Manage installed alp-sdk versions.
    Sdk,
    /// Preflight the host toolchain / environment.
    Doctor,
    /// Validate a board.yaml.
    Validate,
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Commands::Build(args) => commands::build(args),
        Commands::Flash => commands::todo("flash"),
        Commands::Image => commands::todo("image"),
        Commands::Size => commands::todo("size"),
        Commands::Renode => commands::todo("renode"),
        Commands::Clean => commands::todo("clean"),
        Commands::Sdk => commands::todo("sdk"),
        Commands::Doctor => commands::todo("doctor"),
        Commands::Validate => commands::todo("validate"),
    }
}
