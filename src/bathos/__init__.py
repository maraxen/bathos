__version__ = "0.13.0a5"

from bathos.compact import CompactionLockedError, CorruptDatabaseError
from bathos.decorators import experiment

__all__ = ["experiment", "CorruptDatabaseError", "CompactionLockedError"]
