// SPDX-License-Identifier: Apache-2.0
//! Issue #187: `tan init` prompted even with no terminal to prompt on, and
//! blocked FOREVER — no timeout, no diagnostic, no exit, nothing created. The
//! only output was the prompt itself, bracketed-paste (`?2004h`) and
//! cursor-hide (`?25l`) escapes and all, written into whatever stderr happened
//! to be.
//!
//! Note WHERE it blocked, because it is not where the report assumed: inquire
//! renders to stderr and reads through crossterm's `tty_fd()`, which falls back
//! to opening `/dev/tty` when stdin is not a terminal. So `tan init </dev/null`
//! from a real terminal session blocked on `/dev/tty`, not on the redirected
//! stdin; `init: Cancelled.` exit 1 is what you get only where `/dev/tty`
//! cannot be opened at all (a CI runner, an agent shell). Both are wrong, and
//! `GlobalArgs::can_prompt()` now requires stdin AND stderr to be terminals.
//!
//! A subprocess suite, not a unit test, for two reasons. First, the bug is a
//! property of the PROCESS, not of a function: `can_prompt()` reads the real
//! stdio handles, and only a spawned child can be handed non-TTY ones. Second,
//! the failure mode is a hang — a unit test that regressed would block the
//! whole `cargo test` run instead of failing, so every case here is driven
//! through `run_with_timeout`, which kills the child and fails loudly. (One
//! deliberate exception is documented at
//! `commands::init::resolve::tests::a_given_name_answers_the_destination_question`.)
//!
//! The issue's own reproduction is `tan init --from-example
//! peripheral-io/uart-echo --name my-app </dev/null`;
//! `from_example_with_name_does_not_hang_on_redirected_stdin` is that command,
//! verbatim, against a synthetic SDK tree.
//!
//! What this suite CANNOT cover: the prompting branch itself. Every case here
//! drives `can_prompt() == false`, and no CI runner in `.github/workflows/
//! ci.yml` has a TTY, so an `is_terminal()` FALSE NEGATIVE — `tan init` quietly
//! scaffolding a default instead of asking — would sail past all four gates
//! green. The interactive path stays a manual check; re-run the reporter's own
//! Git Bash/MSYS session before closing #187.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// Generous enough that a cold debug-profile binary on a loaded CI runner never
/// trips it, short enough that a regression fails the job in seconds rather
/// than sitting until the runner's own global timeout. The bug it guards was
/// observed as unbounded — the reporter killed it externally at 25s and the
/// process still never exited on its own.
const TIMEOUT: Duration = Duration::from_secs(60);

/// A scratch directory for one case, nested under its own fresh parent so
/// nothing else in the shared temp root can be mistaken for a sibling
/// `alp-sdk/` by the CLI's workspace auto-discovery (same reasoning as
/// `contract.rs`'s and `sdk_ancestor_discovery.rs`'s `fresh_dir`).
fn fresh_dir(tag: &str) -> PathBuf {
    let parent = std::env::temp_dir().join(format!("tan-nontty-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&parent);
    let dir = parent.join("root");
    std::fs::create_dir_all(&dir).expect("create scratch dir");
    dir
}

/// Outcome of one timed `tan` run: the process exit code plus its stderr (text
/// mode prints every line there, see `emit` in `main.rs`).
struct Run {
    code: i32,
    stderr: String,
}

/// Spawn `tan <args>` in `cwd` with stdin wired to `Stdio::null()` and stderr
/// captured — non-TTY on both handles, exactly what `</dev/null`, a CI step, an
/// IDE task runner and another tool's `Command::output()` all hand it — and
/// fail the test if it has not exited within `TIMEOUT`.
///
/// `try_wait` polling rather than `output()`: `output()` waits forever, which is
/// precisely the behaviour under test, so a regression would hang the suite
/// instead of reporting the defect. The pipes are drained on their own threads
/// for the same reason — an undrained child that fills the pipe buffer (~4 KiB
/// on Windows, 64 KiB on Linux) blocks on `write`, which the watchdog would
/// then misreport as the prompt hang.
///
/// HOME/USERPROFILE are redirected at an empty scratch dir so a developer's
/// real `~/.alp/sdk-default` cannot leak into SDK resolution and add a `.alp/`
/// pin to the assertions below — the same isolation `contract.rs`'s `run_case`
/// documents and applies.
fn run_with_timeout(cwd: &Path, args: &[&str]) -> Run {
    let home = cwd.join("__isolated_home");
    std::fs::create_dir_all(&home).expect("create isolated home");

    let mut child = Command::new(env!("CARGO_BIN_EXE_tan"))
        .args(args)
        .current_dir(cwd)
        .env("HOME", &home)
        .env("USERPROFILE", &home)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn tan");

    let mut child_stdout = child.stdout.take().expect("child stdout pipe");
    let mut child_stderr = child.stderr.take().expect("child stderr pipe");
    let drain_stdout = std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = child_stdout.read_to_end(&mut buf);
        buf
    });
    let drain_stderr = std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = child_stderr.read_to_end(&mut buf);
        buf
    });

    let started = Instant::now();
    loop {
        match child.try_wait().expect("poll tan") {
            Some(status) => {
                let err = drain_stderr.join().expect("join stderr drain");
                let _ = drain_stdout.join();
                return Run {
                    code: status.code().unwrap_or(-1),
                    stderr: String::from_utf8_lossy(&err).into_owned(),
                };
            }
            None => {
                if started.elapsed() >= TIMEOUT {
                    let _ = child.kill();
                    let _ = child.wait();
                    panic!(
                        "`tan {}` did not exit within {}s with stdin and stderr both non-TTY — it \
                         is prompting on a terminal that is not there (issue #187)",
                        args.join(" "),
                        TIMEOUT.as_secs()
                    );
                }
                std::thread::sleep(Duration::from_millis(50));
            }
        }
    }
}

/// `scripts/alp_project.py` is the marker SDK discovery probes; `examples/` is
/// what `--from-example` copies out of. Three files is a complete stand-in for
/// a real example as far as this suite is concerned — the copy path is covered
/// elsewhere; what matters here is that the command reaches it at all.
fn make_sdk_root_with_example(dir: &Path, example: &str) {
    std::fs::create_dir_all(dir.join("scripts")).expect("create scripts dir");
    std::fs::write(dir.join("scripts").join("alp_project.py"), "").expect("write loader marker");
    let example_dir = dir.join("examples").join(example);
    std::fs::create_dir_all(example_dir.join("src")).expect("create example dir");
    std::fs::write(
        example_dir.join("board.yaml"),
        "schema_version: \"2\"\nsom:\n  sku: E1M-AEN801\n",
    )
    .expect("write example board.yaml");
    std::fs::write(example_dir.join("CMakeLists.txt"), "# example\n")
        .expect("write example CMakeLists.txt");
    std::fs::write(
        example_dir.join("src").join("main.c"),
        "int main(void){return 0;}\n",
    )
    .expect("write example main.c");
}

#[test]
fn from_example_with_name_does_not_hang_on_redirected_stdin() {
    // The issue's verbatim reproduction. It used to emit only the prompt's
    // ANSI escapes and then block; nothing was created.
    let dir = fresh_dir("from-example");
    let sdk = dir.join("alp-sdk");
    make_sdk_root_with_example(&sdk, "peripheral-io/uart-echo");
    let work = dir.join("work");
    std::fs::create_dir_all(&work).expect("create work dir");

    let run = run_with_timeout(
        &work,
        &[
            "--sdk-root",
            sdk.to_str().expect("sdk path is utf-8"),
            "init",
            "--from-example",
            "peripheral-io/uart-echo",
            "--name",
            "my-app",
        ],
    );

    assert_eq!(run.code, 0, "expected success, stderr:\n{}", run.stderr);
    // The destination prompt is skipped because there is no terminal, so the
    // `.` default stands and `--name` puts the project in `./my-app` — with no
    // `--destination` and no `--non-interactive` incantation. `--name` itself
    // does NOT answer that prompt (#198 corrected #187's report on that
    // point); it only names the subdirectory under whichever destination wins.
    assert!(
        work.join("my-app").join("board.yaml").is_file(),
        "example was not copied into ./my-app, stderr:\n{}",
        run.stderr
    );
    assert!(work.join("my-app").join("src").join("main.c").is_file());
}

#[test]
fn template_init_falls_back_to_flag_defaults_on_redirected_stdin() {
    // No `--template`, no `--name`, no `--destination` — every prompt the
    // command owns is unanswered, and all three must resolve from defaults
    // instead of blocking. `--template`'s help already promised exactly this
    // ("defaults to zephyr-app when not given and there is no TTY to prompt
    // on"); before #187 that promise held only under an explicit
    // `--non-interactive`.
    let dir = fresh_dir("template-defaults");
    let run = run_with_timeout(&dir, &["init"]);

    assert_eq!(run.code, 0, "expected success, stderr:\n{}", run.stderr);
    assert!(
        dir.join("board.yaml").is_file(),
        "template was not scaffolded into the default destination, stderr:\n{}",
        run.stderr
    );
}

#[test]
fn scaffold_reports_the_missing_name_instead_of_prompting_on_redirected_stdin() {
    // `tan scaffold` shares the prompt gate and had the same hang. Its
    // non-interactive contract is to REFUSE (the module name has no sane
    // default), so the fix here is a clean validation failure — exit 2 with
    // `scaffold.name-required` — rather than a default.
    let dir = fresh_dir("scaffold-name");
    let run = run_with_timeout(&dir, &["scaffold"]);

    assert_eq!(
        run.code, 2,
        "expected validation failure, stderr:\n{}",
        run.stderr
    );
    assert!(
        run.stderr.contains("Module name is required"),
        "expected the name-required diagnostic, stderr:\n{}",
        run.stderr
    );
}
