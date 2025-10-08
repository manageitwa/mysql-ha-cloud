"""This file contains the Snapshot related actions"""

import os
import time
import logging
import subprocess

from shutil import rmtree, move
from mcm.utils import Utils
from mcm.mysql import Mysql
from mcm.consul import Consul

class Snapshot:

    pendingPath = "/snapshots/pending"
    currentPath = "/snapshots/current"

    @staticmethod
    def exists():
        """Check if a snapshot exists"""

        checkPaths = [
            os.path.exists(Snapshot.currentPath),
            os.path.exists(f"{Snapshot.currentPath}/xtrabackup_checkpoints"),
            os.path.exists(f"{Snapshot.currentPath}/xtrabackup_binlog_info"),
            os.path.exists(f"{Snapshot.currentPath}/xtrabackup_logfile")
        ]

        return all(checkPaths)

    @staticmethod
    def getTime():
        """Get the time of the current snapshot"""

        if not Snapshot.exists():
            return None

        return os.path.getmtime(Snapshot.currentPath)

    @staticmethod
    def isPending():
        """Check if a snapshot is pending"""

        if not os.path.exists(Snapshot.pendingPath):
            return False

        # Check if any node is snapshotting in Consul
        if Consul.get_instance().are_nodes_snapshotting():
            return True

        # No node is snapshotting according to Consul, reset pending snapshot
        Snapshot.resetPending()
        return False

    @staticmethod
    def waitForSnapshot():
        """Wait for a snapshot to be created"""

        retryCounter = 100

        for _ in range(retryCounter):
            if not Snapshot.isPending() and Snapshot.exists():
                return True

            logging.debug(
                "Still waiting for snapshot (%s, %s)",
                Snapshot.isPending(),
                Snapshot.exists()
            )

            # Keep consul sessions alive
            Consul.get_instance().refresh_sessions()
            time.sleep(5)

        return False

    @staticmethod
    def waitForSnapshotAndRestores():
        """Wait for a snapshot to be created and all nodes to finish restoring"""

        retryCounter = 100

        for _ in range(retryCounter):
            if (Snapshot.exists()
                and not Snapshot.isPending()
                and not Consul.get_instance().are_nodes_restoring()):
                return True

            logging.debug(
                "Still waiting for snapshot and restores (%s, %s, %s)",
                Snapshot.isPending(),
                Snapshot.exists(),
                Consul.get_instance().are_nodes_restoring()
            )

            # Keep consul sessions alive
            Consul.get_instance().refresh_sessions()
            time.sleep(5)

        return False

    @staticmethod
    def create(fromSource = False):
        """Create a snapshot"""

        if Snapshot.isPending() or Consul.get_instance().are_nodes_restoring():
            logging.info("Pending snapshot or restore, wait for it to complete before creating a new snapshot")

            finished = Snapshot.waitForSnapshotAndRestores()

            if not finished:
                logging.error("Snapshot creation / restoration did not finish in time")
                return False

        logging.info("Snapshotting MySQL into dir %s", Snapshot.pendingPath)
        if os.path.exists(Snapshot.pendingPath):
            logging.warning("Snapshot path %s already exists, removing", Snapshot.pendingPath)
            rmtree(Snapshot.pendingPath)

        # Crate backup dir
        os.makedirs(Snapshot.pendingPath)

        try:
            Consul.get_instance().node_set_snapshotting_flag(snapshotting=True)

            # Create mysql backup
            backupUser = Utils.get_envvar_or_secret("MYSQL_BACKUP_USER")
            backupPass = Utils.get_envvar_or_secret("MYSQL_BACKUP_PASSWORD")
            xtrabackup = [Mysql.xtrabackup_binary, f"--user={backupUser}",
                        f"--password={backupPass}", "--backup",
                        f"--target-dir={Snapshot.pendingPath}"]

            if not fromSource:
                xtrabackup.append("--safe-slave-backup")

            subprocess.run(xtrabackup, check=True)

            # Prepare backup
            xtrabackup_prepare = [Mysql.xtrabackup_binary, "--prepare",
                                f"--target-dir={Snapshot.pendingPath}"]

            subprocess.run(xtrabackup_prepare, check=True)

            # Remove old snapshot
            logging.info("Removing old snapshot %s", Snapshot.currentPath)
            if os.path.exists(Snapshot.currentPath):
                rmtree(Snapshot.currentPath)

            move(Snapshot.pendingPath, Snapshot.currentPath)

            Consul.get_instance().node_set_snapshotting_flag(snapshotting=False)

            logging.info("Snapshot was successfully created")
            return True
        except:
            logging.exception("Failed to create snapshot")
            Snapshot.resetPending()
            Consul.get_instance().node_set_snapshotting_flag(snapshotting=False)
            return False

    @staticmethod
    def restore():
        """Restore MySQL server from a snapshot"""

        if not Snapshot.exists():
            logging.error("No snapshot to restore")
            return False

        if Snapshot.isPending():
            logging.info("Pending snapshot, wait for it to complete before restoring")

            finished = Snapshot.waitForSnapshot()

            if not finished:
                logging.error("Snapshot creation did not finish in time")
                return False

        oldMysqlDir = None

        try:
            Consul.get_instance().node_set_restoring_flag(restoring=True)

            logging.info("Restoring snapshot from %s", Snapshot.currentPath)

            if os.path.isfile(f"{Mysql.mysql_datadir}/ib_logfile0"):
                logging.info("MySQL is already initialized, cleaning up first")
                currentTime = time.time()
                oldMysqlDir = f"{Mysql.mysql_datadir}_restore_{currentTime}"

                os.mkdir(oldMysqlDir, 0o700)

                # Renaming file per file, on some docker images
                # the complete directory can not be moved
                for entry in os.listdir(Mysql.mysql_datadir):
                    sourcePath = f"{Mysql.mysql_datadir}/{entry}"
                    destPath = f"{oldMysqlDir}/{entry}"
                    logging.debug("Moving %s to %s", sourcePath, destPath)
                    move(sourcePath, destPath)

                logging.info("Old MySQL data moved to: %s", oldMysqlDir)

            # Restore backup
            xtrabackup = [Mysql.xtrabackup_binary, "--copy-back",
                        f"--target-dir={Snapshot.currentPath}"]
            subprocess.run(xtrabackup, check=True)

            # Change permissions of the restored data
            chown = ['chown', 'mysql.mysql', '-R', '/var/lib/mysql/']
            subprocess.run(chown, check=True)

            # Delete backup MySQL directory
            if oldMysqlDir:
                logging.info("Removing old MySQL data from %s", oldMysqlDir)
                rmtree(oldMysqlDir)

            Consul.get_instance().node_set_restoring_flag(restoring=False)
            return True
        except:
            logging.exception("Failed to restore snapshot")

            if oldMysqlDir:
                logging.info("Restoring old MySQL data from %s", oldMysqlDir)

                for entry in os.listdir(Mysql.mysql_datadir):
                    sourcePath = f"{Mysql.mysql_datadir}/{entry}"
                    rmtree(sourcePath)

                for entry in os.listdir(oldMysqlDir):
                    sourcePath = f"{oldMysqlDir}/{entry}"
                    destPath = f"{Mysql.mysql_datadir}/{entry}"
                    move(sourcePath, destPath)

            Consul.get_instance().node_set_restoring_flag(restoring=False)
            return False

    @staticmethod
    def resetPending():
        """Reset the pending snapshot"""

        logging.info("Removing pending snapshot %s", Snapshot.pendingPath)

        if os.path.exists(Snapshot.pendingPath):
            rmtree(Snapshot.pendingPath)
        else:
            logging.info("No pending snapshot to remove %s", Snapshot.pendingPath)
