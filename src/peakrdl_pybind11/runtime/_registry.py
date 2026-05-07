"""Master extension registry.

Sibling to Unit 1's seam in :mod:`peakrdl_pybind11.masters.base`. A "master
extension" is a callable that wraps a :class:`MasterBase` instance, replacing
its ``read`` and ``write`` methods with policy-enforcing versions. Bus
policies (barriers, caching, retries) all attach this way so the seam is
single and testable.

The registry is opt-in: callers explicitly enable an extension on a master
via :func:`register_master_extension`. The registry is global by name so
extensions can be discovered and toggled from configuration, but each
``Master`` instance carries its own per-extension state through the wrapper
objects returned at registration time.

Sketch §13: bus-layer composition.
"""

from __future__ import annotations

from collections.abc import Callable

from ..masters.base import MasterBase

#: Factory takes a master and returns an object that wraps ``read`` /
#: ``write`` (and, optionally, additional surface). The factory is responsible
#: for monkey-patching the master's ``read`` / ``write`` to delegate to itself
#: so that the wrapping is transparent to callers that already hold a reference
#: to the master.
ExtensionFactory = Callable[[MasterBase], object]

_FACTORIES: dict[str, ExtensionFactory] = {}


def register_master_extension(name: str, factory: ExtensionFactory) -> None:
    """Register a named master-extension factory.

    Args:
        name: Stable identifier (e.g. ``"barrier"``, ``"cache"``, ``"retry"``).
        factory: Callable invoked once per master to install the extension.
            See :data:`ExtensionFactory`.

    Re-registering an existing name overrides the previous factory, which
    keeps test setup ergonomic. Production code should pick names that do
    not collide with built-in policies.
    """
    _FACTORIES[name] = factory


def get_master_extension_factory(name: str) -> ExtensionFactory | None:
    """Return the registered factory for ``name``, or ``None`` if missing."""
    return _FACTORIES.get(name)


def attach_master_extension(name: str, master: MasterBase) -> object:
    """Install the extension named ``name`` onto ``master`` and return it.

    Raises:
        KeyError: if no factory is registered under ``name``.
    """
    factory = _FACTORIES.get(name)
    if factory is None:
        raise KeyError(f"no master extension registered under {name!r}")
    return factory(master)


def clear_master_extensions() -> None:
    """Remove every registered factory. Intended for test setup/teardown."""
    _FACTORIES.clear()
