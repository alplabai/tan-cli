// SPDX-License-Identifier: Apache-2.0
#![doc = include_str!("../README.md")]

mod cli;
mod commands;
mod envelope;
mod exit;
mod progress;
mod style;
mod util;

use clap::Parser;

use cli::{Cli, Command};
use commands::CommandRun;

fn main() {
    let args = Cli::parse();
    let global = args.global;

    let run: CommandRun = match args.command {
        Command::Validate(args) => commands::validate::run(&global, &args),
        Command::Generate => commands::generate::run(&global),
        Command::Doctor(args) => commands::doctor::run(&global, &args),
        Command::Sdk(args) => commands::sdk::run(&global, &args),
        Command::Bootstrap(args) => commands::bootstrap::run(&global, &args),
        Command::Build(args) => commands::build::run_build(&global, &args),
        Command::Image(args) => commands::build::run(&global, "image", &args.args),
        Command::Flash(args) => commands::build::run(&global, "flash", &args.args),
        Command::Clean(args) => commands::build::run(&global, "clean", &args.args),
        Command::Renode(args) => commands::build::run(&global, "renode", &args.args),
    };

    emit(&global.format, run.json.as_deref(), &run.text);
    std::process::exit(run.exit.code());
}

fn emit(format: &cli::Format, json: Option<&str>, text: &[String]) {
    match format {
        cli::Format::Json => {
            if let Some(doc) = json {
                println!("{doc}");
            }
        }
        cli::Format::Text => {
            for line in text {
                eprintln!("{line}");
            }
        }
    }
}
