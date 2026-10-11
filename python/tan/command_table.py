# SPDX-License-Identifier: Apache-2.0
"""The registration table for every `tan` subcommand, with LAZY loaders.

Each ``_load_*`` function holds a plain static ``from tan.commands.x_cmd import x``
statement. That is deliberate: PyInstaller follows only the static import
graph, so a ``pkgutil``/``importlib`` registry would work from source and
produce a frozen ``tan`` that cannot find its own commands (see
``tan.commands.__init__``) -- but an import inside a function body is still a
static import to PyInstaller's analysis. Running none of these functions until a
command is actually needed is what keeps ``tan --version`` and
``tan <cmd> --help`` from importing the other ~30 command modules (tan-cli#1505).

``COMMAND_TABLE`` order is the order the commands appear in ``--help`` and in
every derived tree walk; keep it as-is when adding a command (one loader
function plus one ``CommandSpec`` row).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

#: ``(callback, context_settings)`` -- `context_settings` is `None` unless the
#: command needs Click settings of its own (the three west forwarders).
Loaded = tuple[Callable[..., Any], dict[str, Any] | None]


class CommandSpec(NamedTuple):
    name: str
    rich_help_panel: str
    load: Callable[[], Loaded]


def _load_bootstrap() -> Loaded:
    from tan.commands.bootstrap_cmd import bootstrap  # noqa: PLC0415

    return bootstrap, None


def _load_build() -> Loaded:
    from tan.commands.build_cmd import build  # noqa: PLC0415

    return build, None


def _load_clean() -> Loaded:
    from tan.commands.clean_cmd import clean  # noqa: PLC0415

    return clean, None


def _load_completion() -> Loaded:
    from tan.commands.completion_cmd import completion  # noqa: PLC0415

    return completion, None


def _load_debug_config() -> Loaded:
    from tan.commands.debug_config_cmd import debug_config  # noqa: PLC0415

    return debug_config, None


def _load_diff() -> Loaded:
    from tan.commands.diff_cmd import diff  # noqa: PLC0415

    return diff, None


def _load_doctor() -> Loaded:
    from tan.commands.doctor_cmd import doctor  # noqa: PLC0415

    return doctor, None


def _load_examples() -> Loaded:
    from tan.commands.examples_cmd import examples  # noqa: PLC0415

    return examples, None


def _load_explain() -> Loaded:
    from tan.commands.explain_cmd import explain  # noqa: PLC0415

    return explain, None


def _load_faultdecode() -> Loaded:
    from tan.commands.faultdecode_cmd import faultdecode  # noqa: PLC0415

    return faultdecode, None


def _load_flash() -> Loaded:
    from tan.commands.flash_cmd import flash  # noqa: PLC0415

    return flash, None


def _load_generate() -> Loaded:
    from tan.commands.generate_cmd import generate  # noqa: PLC0415

    return generate, None


def _load_image() -> Loaded:
    from tan.commands.image_cmd import image  # noqa: PLC0415

    return image, None


def _load_init() -> Loaded:
    from tan.commands.init_cmd import init  # noqa: PLC0415

    return init, None


def _load_inspect() -> Loaded:
    from tan.commands.inspect_cmd import inspect  # noqa: PLC0415

    return inspect, None


def _load_kconfig() -> Loaded:
    from tan.commands.kconfig_cmd import kconfig  # noqa: PLC0415

    return kconfig, None


def _load_lock() -> Loaded:
    from tan.commands.west_forward_cmd import FORWARD_CONTEXT_SETTINGS, lock  # noqa: PLC0415

    return lock, FORWARD_CONTEXT_SETTINGS


def _load_migrate() -> Loaded:
    from tan.commands.west_forward_cmd import FORWARD_CONTEXT_SETTINGS, migrate  # noqa: PLC0415

    return migrate, FORWARD_CONTEXT_SETTINGS


def _load_model() -> Loaded:
    from tan.commands.model_cmd import model  # noqa: PLC0415

    return model, None


def _load_monitor() -> Loaded:
    from tan.commands.monitor_cmd import monitor  # noqa: PLC0415

    return monitor, None


def _load_new_som() -> Loaded:
    from tan.commands.new_som_cmd import new_som  # noqa: PLC0415

    return new_som, None


def _load_pinmux() -> Loaded:
    from tan.commands.pinmux_cmd import pinmux  # noqa: PLC0415

    return pinmux, None


def _load_presets() -> Loaded:
    from tan.commands.presets_cmd import presets  # noqa: PLC0415

    return presets, None


def _load_probe() -> Loaded:
    from tan.commands.probe_cmd import probe  # noqa: PLC0415

    return probe, None


def _load_reset() -> Loaded:
    from tan.commands.reset_cmd import reset  # noqa: PLC0415

    return reset, None


def _load_quality() -> Loaded:
    from tan.commands.west_forward_cmd import FORWARD_CONTEXT_SETTINGS, quality  # noqa: PLC0415

    return quality, FORWARD_CONTEXT_SETTINGS


def _load_run() -> Loaded:
    from tan.commands.run_cmd import run  # noqa: PLC0415

    return run, None


def _load_scaffold() -> Loaded:
    from tan.commands.scaffold_cmd import scaffold  # noqa: PLC0415

    return scaffold, None


def _load_sdk() -> Loaded:
    from tan.commands.sdk_cmd import sdk  # noqa: PLC0415

    return sdk, None


def _load_size() -> Loaded:
    from tan.commands.size_cmd import size  # noqa: PLC0415

    return size, None


def _load_support_bundle() -> Loaded:
    from tan.commands.support_bundle_cmd import support_bundle  # noqa: PLC0415

    return support_bundle, None


def _load_trace() -> Loaded:
    from tan.commands.trace_cmd import trace  # noqa: PLC0415

    return trace, None


def _load_validate() -> Loaded:
    from tan.commands.validate_cmd import validate  # noqa: PLC0415

    return validate, None


COMMAND_TABLE: tuple[CommandSpec, ...] = (
    CommandSpec("bootstrap", "Setup", _load_bootstrap),
    CommandSpec("build", "Build & run", _load_build),
    CommandSpec("clean", "Build & run", _load_clean),
    CommandSpec("completion", "Setup", _load_completion),
    CommandSpec("debug-config", "Hardware", _load_debug_config),
    CommandSpec("diff", "Inspect & author", _load_diff),
    CommandSpec("doctor", "Setup", _load_doctor),
    CommandSpec("examples", "Start a project", _load_examples),
    CommandSpec("explain", "Start a project", _load_explain),
    CommandSpec("faultdecode", "Hardware", _load_faultdecode),
    CommandSpec("flash", "Hardware", _load_flash),
    CommandSpec("generate", "Configure", _load_generate),
    CommandSpec("image", "Build & run", _load_image),
    CommandSpec("init", "Start a project", _load_init),
    CommandSpec("inspect", "Inspect & author", _load_inspect),
    CommandSpec("kconfig", "Configure", _load_kconfig),
    CommandSpec("lock", "Configure", _load_lock),
    CommandSpec("migrate", "Configure", _load_migrate),
    CommandSpec("model", "Configure", _load_model),
    CommandSpec("monitor", "Hardware", _load_monitor),
    CommandSpec("new-som", "Inspect & author", _load_new_som),
    CommandSpec("pinmux", "Inspect & author", _load_pinmux),
    CommandSpec("presets", "Start a project", _load_presets),
    CommandSpec("probe", "Hardware", _load_probe),
    CommandSpec("reset", "Hardware", _load_reset),
    CommandSpec("quality", "Configure", _load_quality),
    CommandSpec("run", "Build & run", _load_run),
    CommandSpec("scaffold", "Start a project", _load_scaffold),
    CommandSpec("sdk", "Setup", _load_sdk),
    CommandSpec("size", "Build & run", _load_size),
    CommandSpec("support-bundle", "Inspect & author", _load_support_bundle),
    CommandSpec("trace", "Inspect & author", _load_trace),
    CommandSpec("validate", "Configure", _load_validate),
)
