"""This file contains the actions of the cluster manager"""

import logging
import os
import signal
import subprocess
import sys
import threading
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

    # Whether MySQL has been set to read-only because the Consul session could not be renewed
    fenced: ClassVar[bool] = False

    # Seconds without a session renewal before a writable node is fenced. This is less than the
    # session TTL, so the node is read-only before the session can be invalidated and another node
    # can become the leader.
    fence_after: ClassVar[int] = Consul.session_ttl - 5

    # Seconds between checks of whether the Consul cluster needs to be recovered
    consul_recover_check_interval: ClassVar[int] = 2

    # Seconds the Consul cluster must have had no leader before recovering it. This is longer than
    # the session TTL, so a writable node that cannot renew its session has fenced itself before
    # the cluster is recovered and another node can be promoted.
    consul_recover_after: ClassVar[int] = Consul.session_ttl + 5

    # Seconds the same Consul servers must have been seen as lost and remaining before recovering,
    # so that a brief change in the service DNS is not mistaken for servers being lost. Swarm's
    # restart delay means this has usually passed by the time the replacements have started.
    consul_recover_stable_for: ClassVar[int] = 10

    # Seconds after recovering the Consul cluster before it can be recovered again. This is longer
    # than a recovery takes, so a Consul cluster that is still forming is not discarded.
    consul_recover_interval: ClassVar[int] = 120

    # When the Consul cluster was first seen without a leader, and when it was last recovered
    # (monotonic clock)
    consul_leaderless_since: ClassVar[Optional[float]] = None
    consul_last_recovery: ClassVar[Optional[float]] = None

    # The Consul servers that remained when Consul was last recovered, which a replica must be
    # compared against before it is promoted. Cleared once there is a replication leader.
    consul_recovered_servers: ClassVar[Optional[frozenset[str]]] = None

    # The Consul servers last seen as lost and remaining, and since when (monotonic clock)
    consul_recovery_view: ClassVar[Optional[tuple[frozenset[str], frozenset[str]]]] = None
    consul_recovery_view_since: ClassVar[Optional[float]] = None

    # Whether the Consul agent is being restarted, so that its exit is expected
    consul_restarting: ClassVar[bool] = False

    @staticmethod
    def start_consul_recovery_thread():
        """
        Start the thread that recovers the Consul cluster if it has lost the majority of its
        servers. It runs separately from the main event loop, which retries requests to Consul for
        several minutes while Consul has no leader.
        """
        logging.info("Starting the Consul recovery thread")
        threading.Thread(target=Actions.recover_consul_when_needed, args=(), daemon=True).start()

    @staticmethod
    def recover_consul_when_needed():
        """
        Check regularly whether the Consul cluster needs to be recovered
        """
        while True:
            try:
                Actions.recover_consul_if_needed()
            except Exception:
                Utils.log_repeated(
                    "consul_recovery_check",
                    logging.ERROR,
                    "Unable to check whether Consul needs to be recovered",
                    exc_info=True,
                )

            time.sleep(Actions.consul_recover_check_interval)

    @staticmethod
    def get_other_running_mysql_servers():
        """
        Get the IPs of the service's other tasks that are running MySQL. MySQL only runs once a node
        has its data, so none are running in a cold start of the whole cluster.
        """
        return [
            ip_address
            for ip_address in Consul.get_service_task_ips()
            if ip_address != Consul.getLocalIp() and Mysql.is_server_running(ip_address)
        ]

    @staticmethod
    def recover_consul_if_needed():
        """
        Recover the Consul cluster if it has lost the majority of its servers. Consul cannot elect a
        leader in this case, and as the servers' data is not kept, the lost servers never return,
        so it cannot recover on its own.

        The Consul servers that are not running are only treated as lost if they are no longer tasks
        of the service and Swarm has started replacements, so that Swarm, rather than this node,
        decides that they are gone. If they are still running, such as in a network partition, the
        cluster is left without a leader rather than risk a second Consul cluster being created.

        To recover, the remaining servers discard their Consul data and bootstrap a new Consul
        cluster, which the replacements then join. The MySQL leader is then decided again by
        failover, which promotes the node with the most recent transactions.
        """
        consul = Consul.get_instance()

        if consul.has_leader() is not False:
            Actions.consul_leaderless_since = None
            Actions.consul_recovery_view = None
            Utils.clear_repeated_log("consul_recovery")
            return

        now = time.monotonic()

        if Actions.consul_leaderless_since is None:
            Actions.consul_leaderless_since = now

        leaderless_for = now - Actions.consul_leaderless_since

        try:
            peers = set(consul.get_raft_peer_ips())
        except Exception:
            Utils.log_repeated(
                "consul_recovery",
                logging.WARNING,
                "Unable to read the Consul Raft configuration",
                exc_info=True,
            )
            return

        tasks = set(Consul.get_service_task_ips())
        remaining = peers & tasks
        lost = peers - tasks
        replacements = tasks - peers

        # The servers are checked while waiting for the leader, so that they have usually been the
        # same for long enough by the time recovery is allowed
        view = (frozenset(lost), frozenset(remaining))

        if view != Actions.consul_recovery_view:
            Actions.consul_recovery_view = view
            Actions.consul_recovery_view_since = now

        if leaderless_for < Actions.consul_recover_after:
            return

        if (
            Actions.consul_last_recovery is not None
            and now - Actions.consul_last_recovery < Actions.consul_recover_interval
        ):
            Utils.log_repeated(
                "consul_recovery",
                logging.WARNING,
                "Consul has had no leader for %d seconds since it was recovered, waiting before "
                "recovering it again",
                leaderless_for,
            )
            return

        # Before the agent has joined a Consul cluster, such as in a cold start, it has no servers
        if not peers:
            Utils.log_repeated(
                "consul_recovery",
                logging.INFO,
                "Waiting for the Consul cluster to form",
            )
            return

        if not lost:
            Utils.log_repeated(
                "consul_recovery",
                logging.ERROR,
                "Consul has had no leader for %d seconds, but all of its servers (%s) are still "
                "running according to Swarm. They may be unreachable, such as in a network "
                "partition, so Consul is not recovered automatically.",
                leaderless_for,
                sorted(peers),
            )
            return

        if len(remaining) > len(peers) // 2:
            Utils.log_repeated(
                "consul_recovery",
                logging.WARNING,
                "Consul has had no leader for %d seconds, but a majority of its servers (%s of "
                "%s) are still running, so it should elect a leader without being recovered",
                leaderless_for,
                sorted(remaining),
                sorted(peers),
            )
            return

        if Consul.getLocalIp() not in remaining:
            Utils.log_repeated(
                "consul_recovery",
                logging.INFO,
                "Consul has lost the majority of its servers, waiting for the remaining servers "
                "(%s) to recover it",
                sorted(remaining),
            )
            return

        if not replacements:
            Utils.log_repeated(
                "consul_recovery",
                logging.INFO,
                "Consul has lost the majority of its servers (%s are no longer running), waiting "
                "for Swarm to replace them before recovering",
                sorted(lost),
            )
            return

        if now - Actions.consul_recovery_view_since < Actions.consul_recover_stable_for:
            Utils.log_repeated(
                "consul_recovery",
                logging.INFO,
                "Consul has lost the majority of its servers (%s are no longer running), "
                "recovering once this has not changed for %d seconds",
                sorted(lost),
                Actions.consul_recover_stable_for,
            )
            return

        logging.error(
            "Consul has lost the majority of its servers (%s are no longer running, %s remain, "
            "%s are replacements), recovering the Consul cluster with the remaining servers",
            sorted(lost),
            sorted(remaining),
            sorted(replacements),
        )

        Actions.consul_last_recovery = now
        Actions.consul_leaderless_since = None
        Actions.consul_recovery_view = None
        Utils.clear_repeated_log("consul_recovery")

        # Set before the agent restarts, so that failover cannot run in the new Consul cluster
        # without waiting for the other remaining servers
        Actions.consul_recovered_servers = frozenset(remaining)
        Actions.consul_restarting = True

        try:
            Actions.consul_process = Consul.agent_restart(
                Actions.consul_process, len(remaining)
            )
        finally:
            Actions.consul_restarting = False

        try:
            consul.reset_session()
        except Exception:
            # The session is also recreated by the main event loop if it cannot be renewed
            logging.exception("Unable to register this node with the recovered Consul cluster")

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

        # Start the local consul agent. A new Consul cluster is only bootstrapped in a cold start of the
        # whole cluster. Otherwise the agent only joins the existing Consul cluster, so that a separate
        # Consul cluster is never bootstrapped while other nodes are running, such as when most of
        # the Consul servers have been lost and their replacements start.
        if Actions.get_other_running_mysql_servers():
            consul_process = Consul.agent_start()
        else:
            consul_process = Consul.agent_start(
                Utils.get_envvar_or_secret("CONSUL_BOOTSTRAP_EXPECT", "3")
            )

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
                # Consul may have lost its state while other nodes are still running with data more
                # recent than the snapshot, such as when a new Consul cluster is bootstrapped after
                # most of the Consul servers are lost. Only become the leader from the snapshot if
                # no other task is running MySQL, as in a cold start of the whole cluster.
                running = Actions.get_other_running_mysql_servers()

                if running:
                    logging.warning(
                        "No nodes are registered in Consul, but MySQL is running on other nodes "
                        "(%s), waiting instead of becoming the leader from the snapshot",
                        running,
                    )
                    time.sleep(5)
                    continue

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
        Consul.get_instance().start_session_keepalive_thread()
        Actions.start_fencing_thread()
        Actions.start_consul_recovery_thread()
        Actions.join_main_event_loop()

    @staticmethod
    def start_fencing_thread():
        """
        Start the thread that sets MySQL to read-only if the Consul session cannot be renewed. It runs
        separately from the main event loop, so that it still runs if the main loop is waiting on
        Consul.
        """
        logging.info("Starting the fencing thread")
        threading.Thread(target=Actions.fence_when_needed, args=(), daemon=True).start()

    @staticmethod
    def fence_when_needed():
        """
        Check every second whether MySQL needs to be fenced or unfenced
        """
        while True:
            try:
                Actions.check_fencing()
            except Exception:
                logging.exception("Unable to check fencing")

            time.sleep(1)

    @staticmethod
    def check_fencing():
        """
        Set a writable MySQL server to read-only if the Consul session has not been renewed
        recently. The session may then be invalidated and another node promoted, and two writable
        servers would let the data diverge. Writes are allowed again once the session is renewed, if
        this node is still the replication leader.
        """
        consul = Consul.get_instance()
        renewed_ago = time.monotonic() - consul.last_session_renewal

        if renewed_ago > Actions.fence_after:
            if not Actions.fenced and Mysql.is_writable():
                logging.error(
                    "Consul session has not been renewed for %d seconds, setting MySQL to "
                    "read-only to prevent another leader accepting writes at the same time",
                    renewed_ago,
                )
                Mysql.set_read_only(True)
                Actions.fenced = True
        elif Actions.fenced:
            # If this node is no longer the leader, the main event loop makes it a replica
            if consul.holds_replication_leader_lock():
                logging.warning(
                    "Consul session renewed and still the replication leader, setting MySQL "
                    "to read-write"
                )
                Mysql.set_read_only(False)
            else:
                logging.warning(
                    "Consul session renewed but no longer the replication leader, keeping MySQL "
                    "read-only"
                )

            Actions.fenced = False

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
                and not Actions.consul_restarting
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
                    replication_failure_count = 0
                    replication_lag_count = 0

                    if leader_missing_since is None:
                        logging.warning("Replication leader is missing, starting failover")
                        leader_missing_since = datetime.now()

                    # Check replication health as soon as a new leader is available, so that this
                    # replica is marked unhealthy until it has caught up with the new leader
                    last_replication_health_check = None

                    # A snapshotting replica still publishes its transactions, so that the other
                    # replicas wait for it if it has received transactions that they have not, but
                    # it is not promoted until the snapshot is finished
                    if Actions.try_failover(
                        leader_missing_since, can_promote=not Snapshot.is_snapshotting
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
                    Actions.consul_recovered_servers = None

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
    def wait_for_recovered_servers(registered_nodes, local_ip, leader_missing_since):
        """
        Check that the other servers that remained when Consul was recovered have registered again
        and published their transactions. The Consul registry is empty after a recovery and the
        servers register again at different times, so a replica could otherwise be promoted
        without being compared against a server that has received more transactions.

        A server is only waited for while it is still a task of the service. If its MySQL process
        or Consul agent exits, its container exits, so its transactions are lost anyway. There is
        no time limit, so that a server that is running but cannot register is never skipped
        without an operator deciding to discard its transactions.
        """
        recovered_servers = Actions.consul_recovered_servers

        if not recovered_servers:
            return True

        tasks = Consul.get_service_task_ips()

        # A failed lookup does not list this node, and must not be mistaken for the other servers
        # having stopped
        if local_ip not in tasks:
            Utils.log_repeated(
                "failover_recovered_servers",
                logging.WARNING,
                "Unable to look up the tasks of the service, waiting before failover",
            )
            return False

        published = {
            node_data["ip_address"]
            for node_data in registered_nodes
            if node_data.get("failover_gtids") is not None
            and Mysql.is_valid_gtid_set(node_data["failover_gtids"])
        }
        waiting = sorted((recovered_servers & set(tasks)) - published - {local_ip})

        if not waiting:
            Utils.clear_repeated_log("failover_recovered_servers")
            return True

        if datetime.now() - leader_missing_since > timedelta(seconds=60):
            Utils.log_repeated(
                "failover_recovered_servers",
                logging.ERROR,
                "Still waiting for %s to register in Consul and publish received transactions "
                "before failover. No replication leader can be promoted until they do. Stopping "
                "their containers lets failover continue, but discards any transactions only "
                "they have received.",
                waiting,
            )
        else:
            Utils.log_repeated(
                "failover_recovered_servers",
                logging.INFO,
                "Waiting for %s, which remained when Consul was recovered, to register and "
                "publish received transactions before failover",
                waiting,
            )

        return False

    @staticmethod
    def try_failover(leader_missing_since, can_promote=True):
        """
        Try to become the new replication leader after the leader has gone offline. A replica is
        only promoted once it has applied all transactions received from the old leader, and has
        every transaction from the old leader that the other replicas have, so that no
        transactions are lost. If this replica cannot be promoted, its transactions are still
        published for the other replicas to compare against.
        """
        consul = Consul.get_instance()

        # Publish the transactions received from the old leader so the other replicas can compare
        # against them. Transactions that have not been applied yet are included, so that a replica
        # that is behind on applying them is not mistaken for one that has not received them.
        leader_gtids = Mysql.get_leader_gtids()

        if consul.failover_gtids != leader_gtids:
            consul.node_set_failover_gtids(leader_gtids)

        if not can_promote:
            logging.info("Snapshot in progress, not promoting until it is finished")
            return False

        local_ip = Consul.getLocalIp()
        registered_nodes = consul.get_registered_node_data()

        if not Actions.wait_for_recovered_servers(
            registered_nodes, local_ip, leader_missing_since
        ):
            return False

        # Replicas that are stuck or snapshotting may never publish their transactions, so they
        # are ignored after a while
        wait_expired = datetime.now() - leader_missing_since > timedelta(seconds=30)
        most_recent = True

        for node_data in registered_nodes:
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
                    "Waiting for %s to publish received transactions before failover",
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

        # The relay log is discarded when this replica is promoted, so every received transaction
        # must be applied first
        if not Mysql.has_applied_relay_log():
            logging.info("Waiting for received transactions to be applied before failover")
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
