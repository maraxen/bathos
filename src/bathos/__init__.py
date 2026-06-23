__version__ = "0.12.0"

from bathos.decorators import experiment
from bathos.compact import CorruptDatabaseError, CompactionLockedError

__all__ = ["experiment", "CorruptDatabaseError", "CompactionLockedError"]
