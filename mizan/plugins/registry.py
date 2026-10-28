"""Registry[T] + plugin discovery (plugins.yaml, entry points, ``plugins/`` folder)."""
from __future__ import annotations

import importlib
import importlib.metadata
import logging
import os
from pathlib import Path
from typing import Generic, TypeVar

import yaml

log = logging.getLogger("mizan.plugins")
T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, name: str, key_attr: str = "kind"):
        self.name = name
        self.key_attr = key_attr
        self._items: dict[str, type[T]] = {}

    def register(self, cls: type[T]) -> type[T]:
        key = getattr(cls, self.key_attr)
        if not key:
            raise ValueError(f"{cls!r} has no {self.key_attr}")
        self._items[key] = cls
        return cls

    def get(self, key: str) -> type[T]:
        try:
            return self._items[key]
        except KeyError:
            raise KeyError(f"unknown {self.name} '{key}'. available: {sorted(self._items)}") from None

    def has(self, key: str) -> bool:
        return key in self._items

    def keys(self) -> list[str]:
        return sorted(self._items)

    def items(self) -> list[tuple[str, type[T]]]:
        return sorted(self._items.items())


adapters: Registry = Registry("adapter")
segmenters: Registry = Registry("segmenter")
normalizers: Registry = Registry("normalizer")
metrics: Registry = Registry("metric", key_attr="id")
exporters: Registry = Registry("exporter")
telemetry_sources: Registry = Registry("telemetry source")

_GROUPS = {
    "adapters": adapters,
    "segmenters": segmenters,
    "normalizers": normalizers,
    "metrics": metrics,
    "exporters": exporters,
    "telemetry": telemetry_sources,
}

_loaded = False


def _import(spec: str):
    mod, _, attr = spec.partition(":")
    obj = importlib.import_module(mod)
    return getattr(obj, attr) if attr else obj


def _register_module(group: str, spec: str) -> None:
    """``module:Class`` registers a class; ``module`` alone registers every strategy it defines."""
    reg = _GROUPS[group]
    try:
        obj = _import(spec)
    except Exception as e:  # a broken optional plugin must not take the app down
        log.warning("plugin %s/%s failed to load: %s", group, spec, e)
        return
    classes = [obj] if isinstance(obj, type) else [
        v for v in vars(obj).values() if isinstance(v, type) and getattr(v, reg.key_attr, "")
        and v.__module__ == obj.__name__
    ]
    for c in classes:
        reg.register(c)


def load_plugins(plugins_file: str | os.PathLike | None = None, force: bool = False) -> None:
    global _loaded
    if _loaded and not force:
        return
    from mizan.plugins import builtin  # noqa: F401  (registers shipped strategies)

    builtin.register_all()
    if plugins_file and Path(plugins_file).exists():
        cfg = yaml.safe_load(Path(plugins_file).read_text()) or {}
        for group in _GROUPS:
            for entry in cfg.get(group, []) or []:
                _register_module(group, entry["module"] if isinstance(entry, dict) else entry)
    for group in _GROUPS:
        for ep in importlib.metadata.entry_points(group=f"mizan.{group}"):
            _register_module(group, ep.value)
    _loaded = True
