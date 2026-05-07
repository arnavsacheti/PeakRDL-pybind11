"""
``--strict-fields=<bool>`` CLI flag (Unit 24).

This module is the build-time toggle described in
``IDEAL_API_SKETCH.md`` §3.4 / §22.8. The default is **strict**: bare
attribute assignment on a register outside a context-manager raises
:class:`AttributeError` with a hint pointing to the canonical RMW or
batch idioms.

When the user opts out via ``--strict-fields=false``, the generated
runtime module:

* emits a :class:`DeprecationWarning` at module import time;
* falls back to ``modify(field=value)`` semantics on a bare attribute
  assignment — and emits a per-assignment :class:`DeprecationWarning`
  every time that path fires.

The noise is deliberate. Silent RMW on attribute-assignment is the
single most common source of "I thought that wrote" bugs the sketch
calls out, and per the sketch the warning is the price of admission.

This module owns no ``handle()`` (it does not preempt the export);
instead it parses the flag and the exporter consults
:func:`is_strict_from_options` when rendering ``runtime.py.jinja``.
"""

from __future__ import annotations

import argparse

__all__ = [
    "STRICT_FIELDS_DEFAULT",
    "add_arguments",
    "is_strict_from_options",
    "parse_strict_fields_value",
]

# The sketch is unambiguous: strict is the default, opt-out is loud.
# Surfaced as a constant so tests / documentation can reference it.
STRICT_FIELDS_DEFAULT: bool = True


_TRUE_LITERALS: frozenset[str] = frozenset({"true", "yes", "on", "1"})
_FALSE_LITERALS: frozenset[str] = frozenset({"false", "no", "off", "0"})


def parse_strict_fields_value(value: str | bool | None) -> bool:
    """Parse the string the user typed into a strict-fields boolean.

    Accepts the obvious literals (``true``/``false``/``yes``/``no``/
    ``on``/``off``/``1``/``0``) case-insensitively, plus pre-coerced
    booleans (so calling code does not need to re-check the type).
    Anything else raises :class:`argparse.ArgumentTypeError` so
    argparse surfaces a clean error to the user instead of a stack
    trace from deep inside the exporter.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return STRICT_FIELDS_DEFAULT
    text = value.strip().lower()
    if text in _TRUE_LITERALS:
        return True
    if text in _FALSE_LITERALS:
        return False
    raise argparse.ArgumentTypeError(
        f"--strict-fields expected one of "
        f"{sorted(_TRUE_LITERALS | _FALSE_LITERALS)!r}, got {value!r}"
    )


def add_arguments(arg_group: argparse._ActionsContainer) -> None:
    """Register the ``--strict-fields=<bool>`` flag on ``arg_group``.

    Uses ``=``-style on purpose: the sketch documents the flag as
    ``--strict-fields=false`` (with the equals), which argparse handles
    transparently as long as the option takes a value.
    """
    arg_group.add_argument(
        "--strict-fields",
        dest="strict_fields",
        type=parse_strict_fields_value,
        default=STRICT_FIELDS_DEFAULT,
        metavar="BOOL",
        help=(
            "Build-time toggle for register attribute-assignment policy "
            "(default: true). When 'true', `reg.field = value` outside a "
            "`with reg as r:` context raises with a hint. When 'false', "
            "such assignments fall back to `reg.modify(field=value)` and "
            "emit a DeprecationWarning at module import and per loose "
            "assignment. The opt-out is for porting C drivers that depend "
            "on attribute-assign-as-RMW; new code should leave this true."
        ),
    )


def is_strict_from_options(options: argparse.Namespace | None) -> bool:
    """Convenience: read ``options.strict_fields`` with a safe default.

    Other modules (notably the exporter) call this to decide what to
    bake into the generated runtime. Falling back to the default means
    callers that build options dicts by hand for tests do not have to
    remember to set the flag.
    """
    if options is None:
        return STRICT_FIELDS_DEFAULT
    return bool(getattr(options, "strict_fields", STRICT_FIELDS_DEFAULT))


# No ``handle()`` / ``post_handle()`` — this flag mutates the export,
# it does not preempt or extend it. The exporter calls
# :func:`is_strict_from_options` directly.
