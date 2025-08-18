"""This file contains the Snapshot related actions"""

import os
import time
import logging

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from mcm.consul import Consul

class Snapshot:

    pendingPath = "/snapshots/pending"
    currentPath = "/snapshots/current"

    @staticmethod
    def exists():
        """
        Check if a snapshot exists
        """
        return os.path.exists(Snapshot.currentPath)

    @staticmethod
    def isPending():
        """
        Check if a snapshot is pending
        """
        return os.path.exists(Snapshot.pendingPath)

    @staticmethod
    def waitForSnapshot(consul):
        """
        Wait for a snapshot to be created
        """
        retryCounter = 100

        for _ in range(retryCounter):
            if not Snapshot.isPending() and Snapshot.exists():
                return True

            # Keep consul sessions alive
            Consul.refresh_sessions()
            time.sleep(5)

        return False
