__version__ = "0.13.0a1"

from bathos.decorators import experiment
from bathos.compact import CorruptDatabaseError, CompactionLockedError

__all__ = ["experiment", "CorruptDatabaseError", "CompactionLockedError"]
