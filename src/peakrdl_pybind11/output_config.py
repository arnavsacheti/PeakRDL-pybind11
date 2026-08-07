"""Generated-output selection for :mod:`peakrdl_pybind11`.

The default ``full`` profile deliberately describes the historical output
manifest. ``compact`` removes only offline/legacy copies, preserving metadata
that changes runtime behaviour. ``minimal`` retains only the functional
Python package, C++ sources, CMakeLists.txt, and pyproject.toml.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

OutputProfile = Literal["full", "compact", "minimal"]


@dataclass(frozen=True)
class OutputConfig:
    """Immutable selection of optional generated artifacts."""

    gen_pyi: bool = True
    gen_schema: bool = True
    gen_interrupts: bool = True
    gen_aliases: bool = True
    root_mirror: bool = True

    @classmethod
    def full(cls) -> OutputConfig:
        """Return the backward-compatible historical output manifest."""
        return cls()

    @classmethod
    def compact(cls) -> OutputConfig:
        """Omit offline schema and root mirrors, retaining runtime metadata."""
        return cls(gen_schema=False, root_mirror=False)

    @classmethod
    def minimal(cls) -> OutputConfig:
        """Retain only files required to build and use the generated package."""
        return cls(
            gen_pyi=False,
            gen_schema=False,
            gen_interrupts=False,
            gen_aliases=False,
            root_mirror=False,
        )

    @classmethod
    def from_profile(cls, profile: OutputProfile | str) -> OutputConfig:
        """Resolve a named profile, rejecting misspellings early."""
        profiles = {
            "full": cls.full,
            "compact": cls.compact,
            "minimal": cls.minimal,
        }
        try:
            factory = profiles[profile]
        except KeyError as exc:
            choices = ", ".join(profiles)
            raise ValueError(f"unknown output profile {profile!r}; expected one of: {choices}") from exc
        return factory()

    def with_overrides(
        self,
        *,
        gen_pyi: bool | None = None,
        gen_schema: bool | None = None,
        gen_interrupts: bool | None = None,
        gen_aliases: bool | None = None,
        root_mirror: bool | None = None,
    ) -> OutputConfig:
        """Return a copy with non-``None`` explicit values applied."""
        updates = {
            name: value
            for name, value in {
                "gen_pyi": gen_pyi,
                "gen_schema": gen_schema,
                "gen_interrupts": gen_interrupts,
                "gen_aliases": gen_aliases,
                "root_mirror": root_mirror,
            }.items()
            if value is not None
        }
        return replace(self, **updates)


__all__ = ["OutputConfig", "OutputProfile"]
