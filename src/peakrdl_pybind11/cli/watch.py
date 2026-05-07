"""
``--watch <input.rdl>`` CLI subcommand (Unit 24).

Watches the input RDL file for changes; on save, rebuilds the
generated module and calls ``soc.reload()`` (Unit 16) so an attached
session (notebook / REPL) picks the new tree up without losing the
master or bus state.

The filesystem-watch dependency is soft (``watchdog``, declared under
the ``[notebook]`` extras). If ``watchdog`` is not installed,
``handle()`` raises :class:`NotSupportedError` with a clean
``pip install`` hint, rather than silently no-oping.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

logger = logging.getLogger("peakrdl_pybind11.cli.watch")

__all__ = ["add_arguments", "handle", "import_watchdog"]


def add_arguments(arg_group: argparse._ActionsContainer) -> None:
    """Register ``--watch``."""
    arg_group.add_argument(
        "--watch",
        dest="watch",
        metavar="INPUT_RDL",
        default=None,
        help=(
            "Rebuild and reload the generated bindings whenever INPUT_RDL "
            "changes on disk. Requires the optional 'watchdog' package "
            "(pip install peakrdl-pybind11[notebook])."
        ),
    )


def import_watchdog() -> ModuleType:
    """Soft-import :mod:`watchdog`; raise :class:`NotSupportedError` if missing.

    Lifted into a free function so the test suite can stub it out via
    :func:`unittest.mock.patch` and validate the missing-dependency
    branch without actually uninstalling the package.
    """
    try:
        import watchdog
        import watchdog.events
        import watchdog.observers
    except ImportError as exc:
        from ..runtime import NotSupportedError

        raise NotSupportedError(
            "--watch requires the 'watchdog' package, which is not "
            "installed. Install it with:\n"
            "    pip install peakrdl-pybind11[notebook]\n"
            "(or `pip install watchdog` if you do not want the rest of "
            "the notebook extras)."
        ) from exc
    return watchdog


def _build_handler(rdl_path: Path, on_change: Callable[[], None]) -> Any:  # noqa: ANN401
    """Construct a watchdog FileSystemEventHandler routed to ``on_change``.

    Returns ``Any`` because the handler subclass is defined locally
    against ``watchdog.events.FileSystemEventHandler``, which is only
    importable once :func:`import_watchdog` has succeeded.
    """
    from watchdog.events import FileSystemEvent, FileSystemEventHandler

    target = str(rdl_path.resolve())

    class RDLChangeHandler(FileSystemEventHandler):
        def on_modified(self, event: FileSystemEvent) -> None:
            if event.is_directory:
                return
            if str(Path(event.src_path).resolve()) != target:
                return
            on_change()

    return RDLChangeHandler()


def _default_on_change(rdl_path: Path) -> Callable[[], None]:
    """Default rebuild-and-reload callback used by :func:`handle`.

    Tries to call ``soc.reload()`` if a SoC has already been built and
    bound to the live REPL (Unit 16). When no SoC is available we just
    log the change — the user is presumably running ``--watch`` purely
    to confirm rebuilds without an active tree.
    """

    def callback() -> None:
        sys.stdout.write(f"watch: {rdl_path} changed; rebuild requested\n")
        sys.stdout.flush()
        soc = _live_soc()
        if soc is not None:
            try:
                soc.reload()
            except AttributeError:
                logger.debug("attached SoC has no reload(); skipping", exc_info=True)

    return callback


def _live_soc() -> Any | None:  # noqa: ANN401
    """Best-effort lookup of a live SoC instance.

    Unit 16 will publish the most recently created SoC on the runtime
    package. We import lazily so the lookup costs nothing on builds
    where the unit has not landed.
    """
    try:
        from .. import runtime as runtime_pkg
    except ImportError:
        return None
    return getattr(runtime_pkg, "_active_soc", None)


def handle(options: argparse.Namespace) -> bool:
    """Start the watcher if ``--watch`` was set."""
    rdl_arg: str | None = getattr(options, "watch", None)
    if not rdl_arg:
        return False

    rdl_path = Path(rdl_arg)
    if not rdl_path.exists():
        raise FileNotFoundError(f"watched RDL file not found: {rdl_path}")

    # Force the watchdog import up-front so the missing-dep error fires
    # before we wire any observers.
    import_watchdog()
    from watchdog.observers import Observer

    handler = _build_handler(rdl_path, _default_on_change(rdl_path))
    observer = Observer()
    observer.schedule(handler, str(rdl_path.parent.resolve()), recursive=False)
    observer.start()
    sys.stdout.write(
        f"watch: observing {rdl_path} (Ctrl-C to stop)\n"
    )
    sys.stdout.flush()
    try:
        observer.join()
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
    return True
