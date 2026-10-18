__version__ = "0.13.0a6"

__all__ = ["experiment", "CorruptDatabaseError", "CompactionLockedError"]

# Loaded on first attribute access (PEP 562), not at import: `bathos.hooks.*` runs before every
# tool call and must import only the standard library, so importing `bathos` may not pull in
# duckdb, pyarrow or cisternal.
_LAZY = {
    "experiment": ("bathos.decorators", "experiment"),
    "CorruptDatabaseError": ("bathos.compact", "CorruptDatabaseError"),
    "CompactionLockedError": ("bathos.compact", "CompactionLockedError"),
}


def __getattr__(name: str):
    try:
        module_name, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module 'bathos' has no attribute {name!r}") from None
    import importlib

    value = getattr(importlib.import_module(module_name), attr)
    globals()[name] = value
    return value
