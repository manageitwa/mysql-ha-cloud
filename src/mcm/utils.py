"""This module contains the Utility class for the MCM"""

import os
from datetime import datetime, timedelta


class Utils:
    """
    Utilities class.

    Contains miscellaneous methods that may be globally used through the MCM.
    """

    @staticmethod
    def get_envvar(name: str, default: str | None = None, trim: bool = True) -> str:
        """
        Get the value of an environment variable ONLY (not from a secret file). Should be
        used only for variables that are expected to be file paths. By default, whitespace
        is trimmed.
        """
        if name in os.environ:
            return os.environ[name].strip() if trim else os.environ[name]

        if default is None:
            raise Exception(f"Environment variable {name} not found")
        else:
            return default

    @staticmethod
    def get_envvar_or_secret(
        name: str, default: str | None = None, trim: bool = True
    ) -> str:
        """
        Get the value of an environment variable, or the contents of a secret
        file when using a _FILE suffix. By default, whitespace is trimmed.
        """

        if f"{name}_FILE" in os.environ:
            secret_file = os.environ.get(f"{name}_FILE")
            if secret_file is not None and os.path.exists(secret_file):
                with open(secret_file, "r") as file:
                    return file.read().strip() if trim else file.read()

        return Utils.get_envvar(name, default, trim)

    @staticmethod
    def is_refresh_needed(
        last_execution: datetime | float | None, max_timedelta: timedelta
    ) -> bool:
        """
        Determines if a new execution is needed, based on the time delta.

        The `last_execution` may be a `datetime` instance, a float or None.
        """
        if last_execution is None:
            return True

        ts = (
            last_execution.timestamp()
            if isinstance(last_execution, datetime)
            else last_execution
        )
        return datetime.now().timestamp() - ts > max_timedelta.total_seconds()
