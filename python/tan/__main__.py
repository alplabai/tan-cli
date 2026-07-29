# SPDX-License-Identifier: Apache-2.0
"""The `tan` entrypoint."""
import typer

from tan.version import TAN_VERSION

app = typer.Typer(add_completion=False)


@app.callback(invoke_without_command=True)
def cli(version: bool = typer.Option(False, "--version")) -> None:
    if version:
        # MUST match /^tan \d+\.\d+\.\d+/ -- the extension rejects the binary
        # otherwise (alp-sdk-vscode/src/alpCli/service.ts:107-121).
        typer.echo(f"tan {TAN_VERSION}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
