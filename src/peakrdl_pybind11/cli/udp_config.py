"""
``--udp-config PATH`` parser (sketch §8.2 / §18).

This module is **only the parser**. The ``--udp-config`` flag itself is
registered directly on the exporter's argparse group in
:mod:`peakrdl_pybind11.__peakrdl__` (alongside ``--interrupt-pattern``),
not through the sibling-unit ``add_arguments`` discovery seam — the flag
mutates the export rather than preempting or extending it.

The TOML schema is a top-level ``[udp]`` table mapping UDP names to one
of the supported scalar type names::

    # udp_config.toml
    [udp]
    secure_field = "bool"
    max_value = "int"
    description = "str"

Anything outside ``{"int", "bool", "str", "float"}`` raises
:class:`UDPConfigError` with a clear message. The exporter stores the
parsed mapping on ``self._udp_type_map`` and downstream consumers (e.g.
the .pyi stub generator) use it to annotate ``info.tags.<udp_name>``
with a real type instead of falling back to :data:`typing.Any`.

This commit wires the *declared types* only; no runtime coercion or
validation is performed. The flag is a type-checker hint surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

__all__ = [
    "ALLOWED_UDP_TYPES",
    "TYPE_NAME_TO_PYTHON_TYPE",
    "UDPConfigError",
    "parse_udp_config",
]

# Sketch §8.2 / §18 calls out "first-class wrappers" for common UDPs.
# This commit limits the declared-type vocabulary to scalar primitives;
# anything richer (custom classes, generics, ``Annotated[...]``) is
# rejected so the parser stays a clean type-hint passthrough.
ALLOWED_UDP_TYPES: Final[frozenset[str]] = frozenset({"int", "bool", "str", "float"})

# Mapping the string names users type into the actual Python type
# objects. Surfaced for downstream consumers (stub generator, runtime
# metadata) that prefer the real ``type`` over the string name.
TYPE_NAME_TO_PYTHON_TYPE: Final[dict[str, type]] = {
    "int": int,
    "bool": bool,
    "str": str,
    "float": float,
}


class UDPConfigError(ValueError):
    """Raised when a ``--udp-config`` TOML file is malformed or invalid.

    Subclasses :class:`ValueError` so callers that want to be lenient
    can ``except ValueError`` without depending on this module.
    """


def parse_udp_config(path: str | Path) -> dict[str, str]:
    """Parse a UDP config TOML file and return the ``{udp_name: type_name}`` map.

    Empty file, or one missing the ``[udp]`` section, returns ``{}`` —
    not a crash. The exporter then falls back to the existing permissive
    ``TagsNamespace`` (``Any``-typed UDPs) for that build.

    Parameters
    ----------
    path:
        Filesystem path to a TOML file. Must exist and be readable.

    Returns
    -------
    dict[str, str]
        Mapping of UDP attribute name → declared Python type name (one
        of ``{"int", "bool", "str", "float"}``).

    Raises
    ------
    UDPConfigError
        If the file is malformed TOML, the ``[udp]`` section is not a
        table, a value is not a string, or the declared type is outside
        :data:`ALLOWED_UDP_TYPES`.
    ImportError
        If running on Python < 3.11 (no :mod:`tomllib`). Re-raised with
        a message pointing the user at the version requirement; the
        rest of the package still works on 3.10.
    """
    try:
        import tomllib
    except ImportError as exc:  # pragma: no cover - 3.10 envs
        raise ImportError(
            "--udp-config requires Python 3.11+ (the stdlib `tomllib` module). "
            "Upgrade Python, or omit --udp-config — the rest of peakrdl-pybind11 "
            "still works on 3.10."
        ) from exc

    file_path = Path(path)
    try:
        with file_path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise UDPConfigError(f"failed to parse UDP config TOML at {file_path}: {exc}") from exc

    udp_section = data.get("udp", {})
    if not isinstance(udp_section, dict):
        raise UDPConfigError(
            f"UDP config at {file_path}: expected a [udp] table, got {type(udp_section).__name__}"
        )

    parsed: dict[str, str] = {}
    for udp_name, type_name in udp_section.items():
        if not isinstance(type_name, str):
            raise UDPConfigError(
                f"UDP config at {file_path}: value for '{udp_name}' must be a string "
                f"naming a Python type, got {type(type_name).__name__}: {type_name!r}"
            )
        if type_name not in ALLOWED_UDP_TYPES:
            allowed = sorted(ALLOWED_UDP_TYPES)
            raise UDPConfigError(
                f"UDP config at {file_path}: unknown type {type_name!r} for UDP "
                f"'{udp_name}'. Allowed types: {allowed}. Richer types "
                "(custom classes, generics, Annotated[...]) are out of scope "
                "for --udp-config; use the runtime TagsNamespace instead."
            )
        parsed[udp_name] = type_name

    return parsed
