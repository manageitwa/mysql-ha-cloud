"""This file contains the actions of the cluster manager"""

import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import ClassVar, Optional

from mcm.consul import Consul
from mcm.mysql import Mysql
from mcm.proxysql import Proxysql
from mcm.snapshot import Snapshot
from mcm.utils import Utils


class Actions:
    """The actions of the application"""

    consul_process: ClassVar[Optional[subprocess.Popen[bytes]]] = None
    mysql_process: ClassVar[Optional[subprocess.Popen[bytes]]] = None

    @staticmethod
    def join_or_bootstrap():
        """
        Join the existing cluster or bootstrap a new cluster
        """

        # Start and configure ProxySQL first - its configuration does not depend on Consul or MySQL,
        # so the node passes its health check quickly and can then route queries to the other nodes
        # while it boots
        Proxysql.start_proxysql()
        Proxysql.inital_setup()

        # Start the local consul agent
        consul_process = Consul.agent_start()

        # Keep the ProxySQL backend servers up to date with the cluster. The Consul connection is
        # created first, as it is shared with the update thread.
        Consul.get_instance()
        proxysql = Proxysql()
        proxysql.start_server_update_thread()

        # Check if we have an existing backup to restore
        # Use this backup if exists, or init a new MySQL database
        snapshotExists = Snapshot.exists()

        # Test for unstable environment (other nodes are present and no leader is present)
        # We don't want to become the new leader on the restored backup directly - all registered
        # nodes are counted, including unhealthy ones, as they will promote a new leader from data
        # that is more recent than the snapshot
        #
        # Needs be be checked before Consul.get_instance().register_node() is called
        #
        while Consul.get_instance().get_replication_leader_ip() is None:
            nodes = [
                node_data["ip_address"]
                for node_data in Consul.get_instance().get_registered_node_data()
            ]

            if len(nodes) == 0:
                logging.info("No other nodes detected, we can become the leader")
                break

            logging.warning("Other nodes (%s) detected but no leader, waiting", nodes)
            time.sleep(5)

        # Try to become session leader (needed to decide if we can create a database)
        replication_leader = Consul.get_instance().try_to_become_replication_leader()

        # Keep session alive until we start the main loop
        Consul.get_instance().start_session_auto_refresh_thread()

        # Register the node. Replicas are registered as unhealthy so ProxySQL does
        # not route queries to them until replication is ready.
        Consul.get_instance().replication_unhealthy = not replication_leader
        Consul.get_instance().register_node()

        logging.info(
            "Init local node (leader=%s, backup=%s)", replication_leader, snapshotExists
        )

        needInitialSnapshot = False

        if replication_leader and not snapshotExists:
            Mysql.init_database_if_needed()
            needInitialSnapshot = True
        elif not replication_leader and not snapshotExists:
            logging.info("We are not the replication leader, waiting for backups")
            snapshotExists = Snapshot.waitForSnapshot()

            if not snapshotExists:
                logging.error(
                    "No snapshot available, please check master logs or incomplete snapshot, exiting"
                )
                sys.exit(1)

            Mysql.restore_backup_or_exit()
        else:
            Mysql.restore_backup_or_exit()

        # Start MySQL
        mysql_process = Mysql.server_start()

        # Check replication user - only on the leader, as the user is replicated to the replicas and
        # changes made on a replica would be transactions that the other replicas do not have
        if replication_leader:
            Mysql.check_replication_user_privileges()
            Mysql.check_root_accounts()

        # Get data from MySQL
        mysql_version = Mysql.execute_query_as_root("SELECT version()")[0]["version()"]
        server_id = Mysql.execute_query_as_root("SELECT @@GLOBAL.server_id")[0][
            "@@GLOBAL.server_id"
        ]

        Consul.get_instance().populate_node_info(
            mysql_version=mysql_version, server_id=server_id
        )

        # Remove the old replication configuration (e.g., from backup)
        Mysql.delete_replication_config()

        # Register service as leader or follower
        Consul.get_instance().register_service(replication_leader)

        # Session keep alive will be handled by the main event loop
        Consul.get_instance().stop_session_auto_refresh_thread()

        # Make an initial snapshot from leader
        if needInitialSnapshot:
            Snapshot.create(fromSource=True)

        # Run the main event loop
        Actions.consul_process = consul_process
        Actions.mysql_process = mysql_process
        Actions.join_main_event_loop()

    @staticmethod
    def join_main_event_loop():
        """
        The main event loop for the join_or_bootstrap action
        """

        last_backup_check = None
        last_session_refresh = None
        last_replication_leader_check = None
        last_replication_health_check = None
        last_proxysql_check = None
        proxysql_failure_count = 0
        max_proxysql_failures = 3
        leader_missing_since = None
        replication_failure_count = 0
        max_replication_failures = 12
        replication_lag_count = 0
        max_replication_lag_checks = 36

        # Main Loop, heavy operations needs to be dispatched
        # to an extra thread. The loop needs to refresh the
        # Consul sessions every few seconds.
        while True:
            if (
                Actions.consul_process is not None
                and Actions.consul_process.poll() is not None
            ):
                logging.error(
                    "Consul process has exited with code %s, exiting",
                    Actions.consul_process.returncode,
                )
                sys.exit(1)

            if (
                Actions.mysql_process is not None
                and Actions.mysql_process.poll() is not None
            ):
                logging.error(
                    "MySQL process has exited with code %s, exiting",
                    Actions.mysql_process.returncode,
                )
                sys.exit(1)

            # ProxySQL is started as a daemon, so check that it is still running and configured
            if Utils.is_refresh_needed(last_proxysql_check, timedelta(seconds=5)):
                last_proxysql_check = datetime.now()

                if Proxysql.is_configured():
                    proxysql_failure_count = 0
                else:
                    proxysql_failure_count += 1

                    if proxysql_failure_count >= max_proxysql_failures:
                        logging.error(
                            "ProxySQL has failed %d consecutive checks, exiting",
                            proxysql_failure_count,
                        )
                        sys.exit(1)

            # Try to replace a failed replication leader. Check every second, so that a missing
            # leader is noticed, replaced and followed as quickly as possible.
            if Utils.is_refresh_needed(
                last_replication_leader_check, timedelta(seconds=1)
            ):
                last_replication_leader_check = datetime.now()

                leader_ip = Consul.get_instance().get_replication_leader_ip()

                replication_leader = Consul.get_instance().is_replication_leader()

                if replication_leader:
                    replication_failure_count = 0
                    replication_lag_count = 0
                elif leader_ip is None:
                    # The leader has gone offline, so replication cannot be healthy until a new leader
                    # is promoted. Do not count failures, and try to become the new leader instead.
                    # Do not promote if currently snapshotting.
                    replication_failure_count = 0
                    replication_lag_count = 0

                    if leader_missing_since is None:
                        logging.warning("Replication leader is missing, starting failover")
                        leader_missing_since = datetime.now()

                    # Check replication health as soon as a new leader is available, so that this
                    # replica is marked unhealthy until it has caught up with the new leader
                    last_replication_health_check = None

                    if not Snapshot.is_snapshotting and Actions.try_failover(
                        leader_missing_since
                    ):
                        replication_leader = True
                elif not Utils.is_refresh_needed(
                    last_replication_health_check, timedelta(seconds=5)
                ):
                    # Replication health is checked every 5 seconds
                    pass
                else:
                    last_replication_health_check = datetime.now()

                    # Allow an unhealthy replica 60 seconds to restart before killing it off
                    replication_healthy = Mysql.is_replication_healthy()

                    if replication_healthy:
                        replication_failure_count = 0
                    else:
                        replication_failure_count += 1

                        if replication_failure_count >= max_replication_failures:
                            logging.error(
                                "Replication has failed %d consecutive checks, "
                                "exiting to allow container restart",
                                replication_failure_count,
                            )
                            sys.exit(1)

                    if Mysql._replication_lagging:
                        replication_lag_count += 1

                        if replication_lag_count >= max_replication_lag_checks:
                            logging.error(
                                "Replica has been lagging for %d consecutive checks, "
                                "exiting to allow container restart",
                                replication_lag_count,
                            )
                            sys.exit(1)
                    else:
                        replication_lag_count = 0

                # A leader is available, so any failover has finished
                if leader_ip is not None:
                    leader_missing_since = None

                    if Consul.get_instance().failover_gtids is not None:
                        Consul.get_instance().node_set_failover_gtids(None)

                # Check for correct replication leader (skip during snapshot)
                if not replication_leader and not Snapshot.is_snapshotting:
                    real_leader = Consul.get_instance().get_replication_leader_ip()
                    configured_leader = Mysql.get_replication_leader_ip()

                    if real_leader is not None and real_leader != configured_leader:
                        logging.info(
                            "Replication leader change (old=%s, new=%s)",
                            configured_leader,
                            real_leader,
                        )
                        Mysql.change_to_replication_client(real_leader)

            # Keep Consul sessions alive
            if Utils.is_refresh_needed(last_session_refresh, timedelta(seconds=5)):
                Consul.get_instance().refresh_sessions()
                last_session_refresh = datetime.now()

            # Create MySQL Backups (using extra thread for backup)
            if Utils.is_refresh_needed(last_backup_check, timedelta(minutes=1)):
                Consul.get_instance().start_session_auto_refresh_thread()
                Mysql.create_backup_if_needed()
                last_backup_check = datetime.now()
                Consul.get_instance().stop_session_auto_refresh_thread()

            time.sleep(1)

    @staticmethod
    def try_failover(leader_missing_since):
        """
        Try to become the new replication leader after the leader has gone offline. A replica is
        only promoted once it has applied all transactions received from the old leader, and has
        every transaction from the old leader that the other replicas have, so that no
        transactions are lost.
        """
        consul = Consul.get_instance()

        if not Mysql.has_applied_relay_log():
            logging.info("Waiting for the relay log to be applied before failover")
            return False

        # Publish the transactions received from the old leader so the other replicas can compare
        # against them
        leader_gtids = Mysql.get_leader_gtids()

        if consul.failover_gtids != leader_gtids:
            consul.node_set_failover_gtids(leader_gtids)

        # Replicas that are stuck or snapshotting may never publish their transactions, so they
        # are ignored after a while
        wait_expired = datetime.now() - leader_missing_since > timedelta(seconds=30)
        local_ip = Consul.getLocalIp()
        most_recent = True

        for node_data in consul.get_registered_node_data():
            ip_address = node_data["ip_address"]

            if ip_address == local_ip or node_data.get("restoring") is True:
                continue

            node_gtids = node_data.get("failover_gtids")

            # Treat an invalid set as not yet published, rather than failing the failover
            if node_gtids is not None and not Mysql.is_valid_gtid_set(node_gtids):
                logging.warning(
                    "Replica %s has published invalid transactions, ignoring them",
                    ip_address,
                )
                node_gtids = None

            if node_gtids is None:
                if wait_expired:
                    continue

                logging.info(
                    "Waiting for %s to publish executed transactions before failover",
                    ip_address,
                )
                most_recent = False
                break

            if not Mysql.is_gtid_subset(node_gtids, leader_gtids):
                logging.info(
                    "Replica %s has received transactions that we have not, not promoting",
                    ip_address,
                )
                most_recent = False
                break

        if not most_recent:
            return False

        # Stop serving queries while being promoted, so that ProxySQL does not send writes to this
        # server before it is writable. This is published before becoming the leader, so that
        # ProxySQL never sees this server as the leader and available until it is writable.
        Mysql.set_replication_unhealthy_flag(True)

        if not consul.try_to_become_replication_leader():
            return False

        Mysql.delete_replication_config()
        Mysql.set_replication_unhealthy_flag(False)
        consul.register_service(True)

        # The root accounts are otherwise only checked when a leader starts, which may not happen
        # during a rolling update. A failure must not stop the new leader.
        try:
            Mysql.check_root_accounts()
        except Exception:
            logging.exception("Unable to check the root accounts")

        return True

    @staticmethod
    def healthcheck():
        """
        Check that ProxySQL is running and configured, so this node can accept queries. The state
        of MySQL and the rest of the cluster is not checked, as Docker replaces unhealthy containers
        and only adds healthy containers to the service DNS used to discover the other nodes.
        """
        if not Proxysql.is_configured():
            sys.exit(1)

    @staticmethod
    def execute_file():
        """
        Execute an SQL file on the MySQL server and prime the snapshot
        """
        # Check if we have an existing backup to restore
        if Snapshot.exists():
            logging.warning("Snapshot already exists, this will be overwritten")

        Mysql.init_database_if_needed()

        # Start MySQL
        Actions.mysql_process = Mysql.server_start(skip_config_build=True)

        # Run a subprocess to execute the SQL file
        try:
            sql_file_path = Utils.get_envvar_or_secret("SQL_FILE_PATH")
            if not sql_file_path or not os.path.isfile(sql_file_path):
                raise Exception(
                    "SQL_FILE_PATH is not defined or the file does not exist"
                )
        except Exception:
            logging.error(
                "SQL_FILE_PATH environment variable not set or file does not exist"
            )
            sys.exit(1)

        logging.info("Executing SQL file %s", sql_file_path)

        process = subprocess.Popen(
            [
                "mysql",
                "-u",
                Utils.get_envvar_or_secret("MYSQL_USER"),
                "-p" + Utils.get_envvar_or_secret("MYSQL_PASSWORD"),
                "-D",
                Utils.get_envvar_or_secret("MYSQL_DATABASE"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        output = process.communicate("source " + sql_file_path)
        response = output[0]
        errors = output[1]
        logging.debug("SQL file executed, output: %s, errors: %s", response, errors)

        Snapshot.create(fromSource=True, force=True)
        Mysql.server_stop()

    @staticmethod
    def terminate_handler(signum, frame):
        """
        Termination handler for the main event loop
        """

        if signum == signal.SIGCHLD:
            # Child process terminated, ignore
            return

        logging.info("Received signal %s, terminating...", signum)

        if Consul.get_instance().node_health_session is not None:
            Consul.get_instance().stop_session_auto_refresh_thread()
            Consul.get_instance().destroy_session()

        # Leave cluster and stop the consul agent
        if Actions.consul_process is not None:
            subprocess.run(
                ["consul", "leave"],
                env={**os.environ, "CONSUL_HTTP_TOKEN": Consul.get_acl_token()},
            )

            Actions.consul_process.terminate()

        # Stop the MySQL server
        if Actions.mysql_process is not None:
            Mysql.server_stop()

        sys.exit(0)
