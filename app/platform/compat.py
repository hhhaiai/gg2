"""Python version compatibility shims."""

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):
        """Backport of StrEnum for Python 3.10."""

        def _generate_next_value_(name, start, count, last_values):
            """Generate the next value when not specified."""
            return name.lower()

        def __str__(self):
            return str(self.value)


__all__ = ["StrEnum"]
