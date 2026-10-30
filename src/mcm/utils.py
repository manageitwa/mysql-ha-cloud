"""This file contains the utils of the cluster manager"""

import logging
import os
import time

from datetime import datetime

class Utils:
    """
    Utilities for the project
    """

    # The message last logged for each repeated condition, and when it was logged (monotonic clock)
    __repeated_logs = {}

    @staticmethod
    def get_envvar(name, default = None):
        """
        Get the value of an environment variable ONLY (not from a secret file). Should be
        used only for variables that are expected to be file paths.
        """
        if name in os.environ:
            return os.environ[name]

        if default is None:
            raise Exception(f"Environment variable {name} not found")
        else:
            return default

    @staticmethod
    def get_envvar_or_secret(name, default = None):
        """
        Get the value of an environment variable, or the contents of a secret
        file when using a _FILE suffix
        """

        if f"{name}_FILE" in os.environ:
            secret_file = os.environ.get(f"{name}_FILE")
            if secret_file is not None and os.path.exists(secret_file):
                with open(secret_file, "r") as file:
                    return file.read().strip()

        if name in os.environ:
            return os.environ[name]

        if default is None:
            raise Exception(f"Environment variable {name} or secret {name}_FILE not found")
        else:
            return default

    @staticmethod
    def is_refresh_needed(last_execution, max_timedelta):
        """
        Is a new execution needed, based on the time delta
        """
        if last_execution is None:
            return True

        if type(last_execution) is float:
            last_execution = datetime.fromtimestamp(last_execution)

        return datetime.now() - last_execution > max_timedelta

    @staticmethod
    def log_repeated(key, level, message, *args, interval = 60, **kwargs):
        """
        Log a message about a condition that is checked repeatedly. It is only logged when the
        message for the condition changes, or as a reminder once every interval seconds, so that a
        condition that lasts does not flood the logs.
        """
        now = time.monotonic()
        last = Utils.__repeated_logs.get(key)

        if last is not None and last[0] == message and now - last[1] < interval:
            return

        Utils.__repeated_logs[key] = (message, now)
        logging.log(level, message, *args, **kwargs)

    @staticmethod
    def clear_repeated_log(key):
        """
        Mark a repeated condition as over, so that it is logged straight away if it happens again
        """
        Utils.__repeated_logs.pop(key, None)
