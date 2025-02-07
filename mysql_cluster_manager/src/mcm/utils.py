"""This file contains the utils of the cluster manager"""

import os
import time

from datetime import datetime

import netifaces

import mcm.minio.Minio as Minio


class Utils:
    """
    Utilities for the project
    """

    def get_envvar_or_secret(name):
        """
        Get the value of an environment variable, or the contents of a secret
        file when using a _FILE suffix
        """

        if f"{name}_FILE" in os.environ:
            secret_file = os.environ.get(f"{name}_FILE")
            if os.path.exists(secret_file):
                with open(secret_file, "r") as file:
                    return file.read().strip()

        if name in os.environ:
            return os.environ[name]

        raise Exception(f"Environment variable {name} or secret {name}_FILE not found")

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

        return datetime.now() - last_execution > max_timedelta

    @staticmethod
    def wait_for_backup_exists(consul):
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
