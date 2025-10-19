"""Template rendering helpers."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict

from functools import reduce
from operator import mul

from jinja2 import Environment, PackageLoader, StrictUndefined


def _to_dict(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _to_dict(v) for k, v in asdict(value).items()}
    if isinstance(value, (list, tuple)):
        return [_to_dict(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_dict(v) for k, v in value.items()}
    return value


def _c_identifier(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value)


def _snake_case(value: str) -> str:
    result = []
    for ch in value:
        if ch.isupper() and result:
            result.append("_")
        result.append(ch.lower())
    return "".join(result)


def _camel_case(value: str) -> str:
    parts = [part for part in value.replace("_", " ").split() if part]
    return "".join(part.capitalize() for part in parts)


def _hex(value: int, width: int = 0) -> str:
    fmt = "#0{}x".format(width + 2) if width else "#x"
    return format(value, fmt)


def _product(values):
    seq = list(values)
    if not seq:
        return 1
    return reduce(mul, seq, 1)


class TemplateRenderer:
    """Thin wrapper around Jinja2 with helpful filters."""

    def __init__(self) -> None:
        self.env = Environment(
            loader=PackageLoader("peakrdl_pybind", "templates"),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
        )
        self.env.filters.update(
            {
                "c_ident": _c_identifier,
                "snake": _snake_case,
                "camel": _camel_case,
                "hex": _hex,
                "product": _product,
            }
        )

    def render_to_path(self, template: str, context: Dict[str, Any], destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        template_obj = self.env.get_template(template)
        rendered = template_obj.render(**context)
        destination.write_text(rendered.rstrip() + "\n", encoding="utf-8")

    def dataclass_context(self, **kwargs: Any) -> Dict[str, Any]:
        return {key: _to_dict(value) for key, value in kwargs.items()}


__all__ = ["TemplateRenderer"]
