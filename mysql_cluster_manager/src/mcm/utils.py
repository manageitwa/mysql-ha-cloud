"""This file contains the utils of the cluster manager"""

import os
import time

from datetime import datetime, timedelta

import netifaces

class Utils:
    """
    Utilities for the project
    """

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
    def get_local_ip_address():
        """
        Get the local IP Address
        """

        interface = os.getenv('MCM_BIND_INTERFACE', "eth0")
        return netifaces.ifaddresses(interface)[netifaces.AF_INET][0]["addr"]

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
    def wait_for_backup_exists(consul):
        from mcm.minio import Minio

        """
        Wait for a backup to be occour
        """

        Minio.setup_connection()

        retry_counter = 100

        for _ in range(retry_counter):
            backup_exists = Minio.does_backup_exists()

            if backup_exists:
                return True

        # Keep consul sessions alive
        consul.refresh_sessions()
        time.sleep(5000)

        return False
