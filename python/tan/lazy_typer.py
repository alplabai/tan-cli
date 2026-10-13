# SPDX-License-Identifier: Apache-2.0
"""A Typer app / Click group that builds each subcommand on first use.

``tan.cli`` used to import all ~31 command modules at module scope, so every
invocation -- ``tan --version`` included -- paid for the heaviest of them
(tan-cli#1505). Here the Click tree is assembled from ``COMMAND_TABLE`` and a
command's module is imported only when Click asks for that command by name
(dispatch, ``tan <cmd> --help``) or when something walks the whole tree
(``tan --help``, a ``--format`` domain check): ``LazyCommands`` is a mapping, so
every ``group.commands[...]`` / ``sorted(group.commands)`` / ``.items()`` caller
keeps working unchanged; only ``.items()``/``.values()`` pay for everything.

The loaders in ``tan.command_table`` stay plain static ``from ... import``
statements, which is what keeps PyInstaller able to see every command module.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping
from typing import TYPE_CHECKING, Any

import typer
from typer.core import TyperGroup
from typer.main import get_command_from_info
from typer.models import CommandInfo

from tan.command_table import COMMAND_TABLE, CommandSpec

if TYPE_CHECKING:
    import click

#: Captured at import so a swap of `typer.main.get_command` (see
#: `LazyTyper.__call__`) can never make this module call itself.
_REAL_GET_COMMAND = typer.main.get_command

COMMAND_NAMES: tuple[str, ...] = tuple(spec.name for spec in COMMAND_TABLE)


_REGISTER_MSG = (
    "tan commands are not registered with app.command(); add a loader and a "
    "CommandSpec row to tan.command_table.COMMAND_TABLE"
)


class LazyCommands(MutableMapping):  # type: ignore[type-arg]
    """name -> Click command, built on first lookup, in ``COMMAND_TABLE`` order."""

    def __init__(self, build: Callable[[CommandSpec], 'click.Command']) -> None:
        self._build = build
        self._specs = {spec.name: spec for spec in COMMAND_TABLE}
        self._built: dict[str, 'click.Command'] = {}
        self._removed: set[str] = set()
        self._extra: dict[str, 'click.Command'] = {}

    def __getitem__(self, name: str) -> 'click.Command':
        if name in self._extra:
            return self._extra[name]
        if name in self._built:
            return self._built[name]
        if name in self._removed or name not in self._specs:
            raise KeyError(name)
        if name not in self._built:
            self._built[name] = self._build(self._specs[name])
        return self._built[name]

    def __setitem__(self, name: str, command: 'click.Command') -> None:
        if name in self._specs:
            self._removed.discard(name)
            self._built[name] = command
            return
        self._extra[name] = command

    def __delitem__(self, name: str) -> None:
        if name not in self:
            raise KeyError(name)
        self._extra.pop(name, None)
        self._built.pop(name, None)
        if name in self._specs:
            self._removed.add(name)

    def __iter__(self) -> Iterator[str]:
        for name in self._specs:
            if name not in self._removed:
                yield name
        yield from self._extra

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __contains__(self, name: object) -> bool:
        return name in self._extra or (name in self._specs and name not in self._removed)


class LazyGroup(TyperGroup):
    """A ``TyperGroup`` whose ``commands`` is a ``LazyCommands`` when built lazily."""

    def install_lazy_commands(self, build: Callable[[CommandSpec], 'click.Command']) -> None:
        self.commands = LazyCommands(build)


class LazyTyper(typer.Typer):
    """``typer.Typer`` whose subcommands come from ``COMMAND_TABLE``.

    ``registered_commands`` still answers with the full, ordered list of
    ``CommandInfo`` (built on first read) so table-derived tests and tools keep
    their view, and ``typer.main.get_command(app)`` still yields the complete
    eager tree. Only ``__call__`` -- the real ``tan`` entry -- and
    ``lazy_group()`` take the lazy path.
    """

    def __init__(self, *args: Any, command_cls: type | None = None, **kwargs: Any) -> None:
        self._command_cls = command_cls
        self._lazy_build = False
        self._lazy_group: LazyGroup | None = None
        self._infos: dict[str, CommandInfo] = {}
        super().__init__(*args, cls=LazyGroup, **kwargs)

    # `Typer.__init__` assigns `self.registered_commands = []`; absorb it.
    @property
    def registered_commands(self) -> list[CommandInfo]:  # type: ignore[override]
        if self._lazy_build:
            return []
        return [self.command_info(spec) for spec in COMMAND_TABLE]

    @registered_commands.setter
    def registered_commands(self, value: list[CommandInfo]) -> None:
        if value:
            raise TypeError(_REGISTER_MSG)

    def command(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        raise TypeError(_REGISTER_MSG)

    def command_info(self, spec: CommandSpec) -> CommandInfo:
        info = self._infos.get(spec.name)
        if info is None:
            callback, context_settings = spec.load()
            info = CommandInfo(
                name=spec.name,
                cls=self._command_cls,
                context_settings=context_settings,
                callback=callback,
                rich_help_panel=spec.rich_help_panel,
            )
            self._infos[spec.name] = info
        return info

    def _build_command(self, spec: CommandSpec) -> 'click.Command':
        return get_command_from_info(
            self.command_info(spec),
            pretty_exceptions_short=self.pretty_exceptions_short,
            rich_markup_mode=self.rich_markup_mode,
        )

    def lazy_group(self) -> 'click.Command':
        """The Click tree with subcommand modules deferred until looked up.

        Built once per instance; every caller (dispatch, help envelope, the
        `--format` domain walk) shares it.
        """
        if self._lazy_group is None:
            self._lazy_build = True
            try:
                group = _REAL_GET_COMMAND(self)
            finally:
                self._lazy_build = False
            assert isinstance(group, LazyGroup)
            group.install_lazy_commands(self._build_command)
            self._lazy_group = group
        return self._lazy_group

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        group = self.lazy_group()

        def _get_command(typer_instance: typer.Typer) -> 'click.Command':
            # `Typer.__call__` rebuilds the tree via `typer.main.get_command(self)`;
            # hand it the lazy group for THIS app and delegate for anything else.
            if typer_instance is self:
                return group
            return _REAL_GET_COMMAND(typer_instance)

        original = typer.main.get_command
        typer.main.get_command = _get_command  # type: ignore[assignment]
        try:
            return super().__call__(*args, **kwargs)
        finally:
            typer.main.get_command = original
