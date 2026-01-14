"""
DbVerifier - Base class for database verification.

Provides common functionality for verifying database entries:
- Sanitizing DB output (comma-separated strings to lists, removing '@' suffix)
- Comparing expected vs actual values

Subclasses must:
- Set DB_NAME, TABLE_NAME, and LIST_KEYS class attributes
- Implement _get_expected(group_name, expected_params) -> Dict
- Implement verify_not_exists(group_name) -> None
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class DbVerifier(ABC):
    """
    Base class for database verification.

    Provides common sanitization and comparison logic for CONFIG_DB and STATE_DB.

    Subclasses must:
    1. Set DB_NAME and TABLE_NAME class attributes
    2. Set LIST_KEYS for fields that should be converted to sorted lists
    3. Implement _get_expected()

    Args:
        duthandler: DutHandler instance (real or mock)
    """

    # Subclasses must override these
    DB_NAME: str = ""
    TABLE_NAME: str = ""
    LIST_KEYS: List[str] = []

    def __init__(self, duthandler):
        self.duthandler = duthandler

    @abstractmethod
    def _get_expected(self, group_name: str, expected_params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Get expected values for comparison."""
        pass

    @abstractmethod
    def verify_not_exists(self, group_name: str) -> None:
        """
        Verify that a database entry does NOT exist.

        Args:
            group_name: Monitor link group name

        Raises:
            AssertionError: If entry exists.
        """
        pass

    def _sanitize_db_output(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Sanitize database output to common schema.

        Converts:
        - Comma-separated strings to sorted lists for LIST_KEYS
        - Removes '@' suffix from keys (e.g., 'uplinks@' -> 'uplinks')

        Args:
            raw_data: Raw dict from database

        Returns:
            Sanitized dict in common schema.
        """
        sanitized = {}

        for key, value in raw_data.items():
            clean_key = key.replace('@', '')

            if clean_key in self.LIST_KEYS:
                if isinstance(value, str):
                    if value == '' or value.strip() == '':
                        sanitized[clean_key] = []
                    elif ',' in value:
                        sanitized[clean_key] = sorted([i.strip() for i in value.split(',')])
                    else:
                        sanitized[clean_key] = [value.strip()]
                elif isinstance(value, list):
                    sanitized[clean_key] = sorted(value)
                else:
                    raise ValueError(
                        f"{self.DB_NAME} field '{clean_key}' expected str or list, "
                        f"got {type(value).__name__}: {value}"
                    )
            else:
                sanitized[clean_key] = value

        return sanitized

    def _compare(self, expected: Dict[str, Any], actual: Dict[str, Any]) -> List[str]:
        """
        Compare expected vs actual dict. Only checks keys present in expected.

        Args:
            expected: Expected values dict
            actual: Actual values dict from database

        Returns:
            List of mismatch description strings.
        """
        mismatches = []

        for key, exp_val in expected.items():
            # Skip empty description
            if key == 'description' and exp_val == '':
                continue

            if key not in actual:
                mismatches.append(f"{key}: not found in {self.DB_NAME}")
                continue

            act_val = actual[key]

            # Normalize lists for comparison
            if isinstance(exp_val, list):
                exp_val = sorted(exp_val)
            if isinstance(act_val, list):
                act_val = sorted(act_val)

            if exp_val != act_val:
                mismatches.append(f"{key}: expected '{exp_val}', got '{act_val}'")

        return mismatches
