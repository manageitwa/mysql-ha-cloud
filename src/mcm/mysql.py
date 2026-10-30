"""This file is part of the MySQL cluster manager"""

import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from datetime import timedelta

import mysql.connector

from mcm.consul import Consul
from mcm.utils import Utils


class Mysql:
    """
    This class encapsulates all MySQL related things
    """

    xtrabackup_binary = "/usr/bin/xtrabackup"
    mysql_server_binary = "/usr/sbin/mysqld"
    mysqld_binary = "/usr/sbin/mysqld"
    mysql_datadir = "/var/lib/mysql"
    _replication_lagging = False

    @staticmethod
    def init_database_if_needed():
        """
        Init a MySQL and configure permissions.
        """

        logging.info("Init MySQL database directory")

        if os.path.isfile(f"{Mysql.mysql_datadir}/ib_logfile0"):
            logging.info("MySQL is already initialized, skipping")
            return False

        mysql_init = [Mysql.mysqld_binary, "--initialize-insecure", "--user=mysql"]
        subprocess.run(mysql_init, check=True)

        # Start server the first time
        mysql_process = Mysql.server_start(
            use_root_password=False, skip_config_build=True
        )

        # Create application user
        logging.debug("Creating MySQL user for the application")
        application_user = Utils.get_envvar_or_secret("MYSQL_USER")
        appication_password = Utils.get_envvar_or_secret("MYSQL_PASSWORD")

        # Values are passed as parameters so that they are escaped. The host is also passed as a
        # parameter, as the connector does not unescape %% in statements with parameters.
        Mysql.execute_statement_or_exit(
            "CREATE USER %s@%s IDENTIFIED WITH caching_sha2_password BY %s",
            params=(application_user, "%", appication_password),
        )

        # Create backup user
        logging.debug("Creating MySQL user for backups")
        backup_user = Utils.get_envvar_or_secret("MYSQL_BACKUP_USER")
        backup_password = Utils.get_envvar_or_secret("MYSQL_BACKUP_PASSWORD")
        Mysql.execute_statement_or_exit(
            "CREATE USER %s@'localhost' IDENTIFIED WITH caching_sha2_password BY %s",
            params=(backup_user, backup_password),
        )
        Mysql.execute_statement_or_exit(
            "GRANT BACKUP_ADMIN, PROCESS, RELOAD, LOCK TABLES, REPLICATION CLIENT, REPLICATION_SLAVE_ADMIN, "
            "REPLICATION CLIENT ON *.* TO %s@'localhost'",
            params=(backup_user,),
        )
        Mysql.execute_statement_or_exit(
            "GRANT SELECT ON performance_schema.log_status TO %s@'localhost'",
            params=(backup_user,),
        )
        Mysql.execute_statement_or_exit(
            "GRANT SELECT ON performance_schema.keyring_component_status TO %s@'localhost'",
            params=(backup_user,),
        )
        Mysql.execute_statement_or_exit(
            "GRANT SELECT ON performance_schema.replication_group_members TO %s@'localhost'",
            params=(backup_user,),
        )

        # Create replication user
        logging.debug("Creating replication user")
        replication_user = Utils.get_envvar_or_secret("MYSQL_REPLICATION_USER")
        replication_password = Utils.get_envvar_or_secret("MYSQL_REPLICATION_PASSWORD")
        Mysql.execute_statement_or_exit(
            "CREATE USER %s@%s IDENTIFIED WITH caching_sha2_password BY %s",
            params=(replication_user, "%", replication_password),
        )
        Mysql.execute_statement_or_exit(
            "GRANT USAGE, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO %s@%s",
            params=(replication_user, "%"),
        )

        # Set the password for the root user. Remote root access is set up by check_root_accounts().
        logging.debug("Set permissions for the root user")
        root_password = Utils.get_envvar_or_secret("MYSQL_ROOT_PASSWORD")
        Mysql.execute_statement_or_exit(
            "ALTER USER 'root'@'localhost' IDENTIFIED WITH caching_sha2_password BY %s",
            params=(root_password,),
        )

        # Create database if specified
        if Utils.get_envvar_or_secret("MYSQL_DATABASE"):
            database_name = Mysql.quote_identifier(
                Utils.get_envvar_or_secret("MYSQL_DATABASE")
            )
            logging.debug("Setting up initial database")
            Mysql.execute_statement_or_exit(
                sql=f"CREATE DATABASE IF NOT EXISTS {database_name}",
                username="root",
                password=root_password,
            )
            Mysql.execute_statement_or_exit(
                sql=f"GRANT ALL PRIVILEGES ON {database_name}.* TO %s@%s",
                params=(application_user, "%"),
                username="root",
                password=root_password,
            )

        # Shutdown MySQL server
        logging.debug("Inital MySQL setup done, shutdown server..")
        Mysql.execute_statement_or_exit(
            sql="SHUTDOWN", username="root", password=root_password
        )
        mysql_process.wait()

        return True

    @staticmethod
    def build_configuration():
        """
        Build the MySQL server configuratuion.
        """
        server_id = Mysql.choose_server_id()

        outfile = open("/etc/mysql/conf.d/zz_cluster.cnf", "w")
        outfile.write("# DO NOT EDIT - This file was generated automatically\n")
        outfile.write("[mysqld]\n")
        outfile.write(f"server_id={server_id}\n")
        outfile.write("gtid_mode=ON\n")
        outfile.write("enforce-gtid-consistency=ON\n")

        if (
            Utils.get_envvar("MYSQL_TLS_CA", False)
            and Utils.get_envvar("MYSQL_TLS_CERT", False)
            and Utils.get_envvar("MYSQL_TLS_KEY", False)
        ):
            outfile.write(f"ssl_ca={Utils.get_envvar('MYSQL_TLS_CA')}\n")
            outfile.write(f"ssl_cert={Utils.get_envvar('MYSQL_TLS_CERT')}\n")
            outfile.write(f"ssl_key={Utils.get_envvar('MYSQL_TLS_KEY')}\n")

            # TLS is only enforced when the certificates are configured, as replication and ProxySQL
            # only use TLS when they are
            if (
                Utils.get_envvar_or_secret("MYSQL_TLS_REQUIRED", "True").lower()
                == "true"
                or Utils.get_envvar_or_secret("MYSQL_TLS_REQUIRED", "True") == "1"
            ):
                outfile.write("require_secure_transport=ON\n")

        outfile.close()

    @staticmethod
    def change_to_replication_client(leader_ip):
        """
        Make the local MySQL installation to a replication follower
        """

        logging.info("Setting up replication (leader=%s)", leader_ip)

        replication_user = Utils.get_envvar_or_secret("MYSQL_REPLICATION_USER")
        replication_password = Utils.get_envvar_or_secret("MYSQL_REPLICATION_PASSWORD")

        Mysql.execute_query_as_root("STOP REPLICA", discard_result=True)

        if (
            Utils.get_envvar("MYSQL_TLS_CA", False)
            and Utils.get_envvar("MYSQL_TLS_CERT", False)
            and Utils.get_envvar("MYSQL_TLS_KEY", False)
        ):
            Mysql.execute_query_as_root(
                "CHANGE REPLICATION SOURCE TO SOURCE_HOST = %s, "
                "SOURCE_PORT = 3306, "
                "SOURCE_AUTO_POSITION = 1, SOURCE_CONNECT_RETRY = 5, GET_SOURCE_PUBLIC_KEY = 1, "
                "SOURCE_SSL=1, SOURCE_SSL_CA = %s, SOURCE_SSL_CERT = %s, SOURCE_SSL_KEY = %s",
                discard_result=True,
                params=(
                    leader_ip,
                    Utils.get_envvar("MYSQL_TLS_CA"),
                    Utils.get_envvar("MYSQL_TLS_CERT"),
                    Utils.get_envvar("MYSQL_TLS_KEY"),
                ),
            )
        else:
            Mysql.execute_query_as_root(
                "CHANGE REPLICATION SOURCE TO SOURCE_HOST = %s, "
                "SOURCE_PORT = 3306, "
                "SOURCE_AUTO_POSITION = 1, SOURCE_CONNECT_RETRY = 5, GET_SOURCE_PUBLIC_KEY = 1",
                discard_result=True,
                params=(leader_ip,),
            )

        Mysql.execute_query_as_root(
            "START REPLICA USER = %s PASSWORD = %s",
            discard_result=True,
            params=(replication_user, replication_password),
        )

        # Set replicia to read only
        logging.info("Set MySQL-Server mode to read-only")
        Mysql.execute_query_as_root("SET GLOBAL read_only = 1", discard_result=True)
        Mysql.execute_query_as_root(
            "SET GLOBAL super_read_only = 1", discard_result=True
        )

    @staticmethod
    def delete_replication_config():
        """
        Stop the replication
        """
        logging.debug("Removing old replication configuraion")
        Mysql.execute_query_as_root("STOP REPLICA", discard_result=True)
        Mysql.execute_query_as_root("RESET REPLICA ALL", discard_result=True)

        # Accept writes
        logging.info("Set MySQL-Server mode to read-write")
        Mysql.execute_query_as_root(
            "SET GLOBAL super_read_only = 0", discard_result=True
        )
        Mysql.execute_query_as_root("SET GLOBAL read_only = 0", discard_result=True)

    @staticmethod
    def choose_server_id():
        """
        Choose a random server ID that is not used by a running server. A replica ignores
        transactions written by a server with its own server ID, so an ID must not be reused while
        transactions written with it can still be replicated, such as those of a leader that has
        been lost. No record of past server IDs is kept, so a random ID is used, which makes reuse
        very unlikely.
        """
        used_server_ids = set(Mysql.get_other_server_ids())

        while True:
            server_id = secrets.randbelow(4294967295) + 1

            if server_id not in used_server_ids:
                return server_id

    @staticmethod
    def get_other_server_ids():
        """
        Get the server IDs of the MySQL servers running on the service's other tasks
        """
        server_ids = []

        for ip_address in Consul.get_service_task_ips():
            if ip_address == Consul.getLocalIp():
                continue

            cnx = None

            try:
                cnx = mysql.connector.connect(
                    host=ip_address,
                    port=3306,
                    user=Utils.get_envvar_or_secret("MYSQL_REPLICATION_USER"),
                    password=Utils.get_envvar_or_secret("MYSQL_REPLICATION_PASSWORD"),
                    connection_timeout=3,
                )
                cursor = cnx.cursor()
                cursor.execute("SELECT @@GLOBAL.server_id")
                server_ids.append(int(cursor.fetchone()[0]))
            except mysql.connector.Error:
                # MySQL is not running on this task yet
                pass
            finally:
                if cnx:
                    cnx.close()

        return server_ids

    @staticmethod
    def is_server_running(ip_address):
        """
        Check if a MySQL server is accepting connections at the given IP address. MySQL only listens
        once it has been started with its data, so this is false for a node that is still restoring
        a snapshot or initialising.
        """
        try:
            with socket.create_connection((ip_address, 3306), timeout=2):
                return True
        except OSError:
            return False

    @staticmethod
    def is_writable():
        """
        Check if the local MySQL server accepts writes
        """
        return Mysql.execute_query_as_root("SELECT @@GLOBAL.read_only AS ro")[0]["ro"] == 0

    @staticmethod
    def set_read_only(read_only):
        """
        Set the local MySQL server to read-only or read-write. super_read_only also stops users with
        administrative privileges from writing.
        """
        if read_only:
            Mysql.execute_query_as_root("SET GLOBAL read_only = 1", discard_result=True)
            Mysql.execute_query_as_root(
                "SET GLOBAL super_read_only = 1", discard_result=True
            )
        else:
            Mysql.execute_query_as_root(
                "SET GLOBAL super_read_only = 0", discard_result=True
            )
            Mysql.execute_query_as_root("SET GLOBAL read_only = 0", discard_result=True)

    @staticmethod
    def get_replication_leader_ip():
        """
        Get the current replication leader ip
        """
        slave_status = Mysql.execute_query_as_root("SHOW REPLICA STATUS")

        if len(slave_status) != 1:
            return None

        if not "Source_Host" in slave_status[0]:
            logging.error("Invalid output, Source_Host not found %s", slave_status)
            return None

        return slave_status[0]["Source_Host"]

    @staticmethod
    def is_replication_healthy():
        """
        Check that replication is running and all data from the leader
        has been applied locally. If a replication thread has stopped,
        attempt an automatic restart.

        Returns True only when both IO and SQL threads are running
        AND the replica is fully caught up with the leader.
        """

        from mcm.snapshot import Snapshot

        if Snapshot.is_snapshotting:
            logging.debug("Skipping replication health check during snapshot")
            return True

        slave_status = Mysql.execute_query_as_root("SHOW REPLICA STATUS")

        if len(slave_status) != 1:
            Mysql.set_replication_unhealthy_flag(True)
            return False

        status = slave_status[0]

        # Check that the replication threads are running
        io_running = status.get("Replica_IO_Running", "No")
        sql_running = status.get("Replica_SQL_Running", "No")

        if io_running != "Yes" or sql_running != "Yes":
            io_error = status.get("Last_IO_Error", "")
            sql_error = status.get("Last_SQL_Error", "")

            # MySQL retries the connection to the leader itself (every SOURCE_CONNECT_RETRY seconds),
            # so only restart replication if a thread has stopped - restarting while it is
            # connecting can block for several seconds
            if io_running == "Connecting" and sql_running == "Yes":
                logging.warning(
                    "Replication is reconnecting to the leader (IO_Error='%s')", io_error
                )
                Mysql.set_replication_unhealthy_flag(True)
                return False

            logging.warning(
                "Replication is not healthy (IO_Running=%s, SQL_Running=%s, "
                "IO_Error='%s', SQL_Error='%s'), attempting restart",
                io_running,
                sql_running,
                io_error,
                sql_error,
            )

            replication_user = Utils.get_envvar_or_secret("MYSQL_REPLICATION_USER")
            replication_password = Utils.get_envvar_or_secret(
                "MYSQL_REPLICATION_PASSWORD"
            )

            Mysql.execute_query_as_root("STOP REPLICA", discard_result=True)
            Mysql.execute_query_as_root(
                "START REPLICA USER = %s PASSWORD = %s",
                discard_result=True,
                params=(replication_user, replication_password),
            )

            # Allow threads time to start before verifying
            time.sleep(1)

            verify_status = Mysql.execute_query_as_root("SHOW REPLICA STATUS")
            if len(verify_status) == 1:
                new_io = verify_status[0].get("Replica_IO_Running", "No")
                new_sql = verify_status[0].get("Replica_SQL_Running", "No")

                if new_io == "No" or new_sql == "No":
                    logging.error(
                        "Replication restart failed (IO_Running=%s, SQL_Running=%s, "
                        "IO_Error='%s', SQL_Error='%s')",
                        new_io,
                        new_sql,
                        verify_status[0].get("Last_IO_Error", ""),
                        verify_status[0].get("Last_SQL_Error", ""),
                    )
                else:
                    logging.info("Replication restart succeeded")

            Mysql.set_replication_unhealthy_flag(True)
            return False

        # Both threads are running — the replica is healthy unless it is lagging behind
        seconds_behind = status.get("Seconds_Behind_Source")
        lag_threshold = int(
            Utils.get_envvar_or_secret("MYSQL_REPLICATION_LAG_THRESHOLD", "5")
        )
        if (
            lag_threshold > 0
            and seconds_behind is not None
            and seconds_behind > lag_threshold
        ):
            logging.warning("Replica is %s seconds behind source", seconds_behind)
            Mysql._replication_lagging = True
        else:
            logging.debug("Replica is %s seconds behind source", seconds_behind)
            Mysql._replication_lagging = False

        Mysql.set_replication_unhealthy_flag(Mysql._replication_lagging)

        io_state = status.get("Replica_IO_State", "")
        logging.debug("Follower IO state is '%s'", io_state)
        if (
            io_state != "Waiting for source to send event"
            and io_state != "Reconnecting after a failed source event read"
        ):
            return False

        sql_state = status.get("Replica_SQL_Running_State", "")
        logging.debug("Follower SQL state is '%s'", sql_state)
        if sql_state != "Replica has read all relay log; waiting for more updates":
            return False

        return True

    @staticmethod
    def has_applied_relay_log():
        """
        Check that all transactions received from the replication leader have been applied. The
        received and executed transactions are compared, as the SQL thread reports that it has read
        all of the relay log while parallel workers may still be applying transactions. A
        transaction that has only partly been received is not included in the received
        transactions, so it does not prevent this from passing if the leader has gone.
        """
        status = Mysql.execute_query_as_root(
            "SELECT GTID_SUBSET(RECEIVED_TRANSACTION_SET, @@GLOBAL.gtid_executed) AS applied "
            "FROM performance_schema.replication_connection_status"
        )

        # Replication is not configured, so there is nothing to apply
        if len(status) != 1:
            return True

        return status[0]["applied"] == 1

    @staticmethod
    def get_leader_gtids():
        """
        Get the set of transactions on this replica that were received from its replication
        leader, whether or not they have been applied yet. Transactions from other servers are
        excluded, as replicas may have executed transactions locally or inherited them from a
        snapshot of another replica.

        A server that is not replicating, such as a leader whose leadership was lost when Consul
        lost its state, returns the transactions written on itself instead. The replicas of that
        leader return the same server's transactions, so it is promoted again if it has them all.
        """
        slave_status = Mysql.execute_query_as_root("SHOW REPLICA STATUS")

        if len(slave_status) != 1 or not slave_status[0].get("Source_UUID"):
            server_uuid = Mysql.execute_query_as_root("SELECT @@GLOBAL.server_uuid AS uuid")[
                0
            ]["uuid"]

            return Mysql.execute_query_as_root(
                "SELECT GTID_SUBTRACT(@@GLOBAL.gtid_executed, "
                "GTID_SUBTRACT(@@GLOBAL.gtid_executed, %s)) AS gtids",
                params=(f"{server_uuid}:1-9223372036854775806",),
            )[0]["gtids"]

        all_leader_gtids = f"{slave_status[0]['Source_UUID']}:1-9223372036854775806"

        return Mysql.execute_query_as_root(
            "SELECT GTID_SUBTRACT(t.gtids, GTID_SUBTRACT(t.gtids, %s)) AS gtids FROM ("
            "SELECT CONCAT_WS(',', NULLIF(@@GLOBAL.gtid_executed, ''), "
            "NULLIF(RECEIVED_TRANSACTION_SET, '')) AS gtids "
            "FROM performance_schema.replication_connection_status) t",
            params=(all_leader_gtids,),
        )[0]["gtids"]

    @staticmethod
    def is_gtid_subset(subset, superset):
        """
        Check if all transactions in one GTID set are contained in another
        """
        result = Mysql.execute_query_as_root(
            "SELECT GTID_SUBSET(%s, %s) AS is_subset", params=(subset, superset)
        )

        return result[0]["is_subset"] == 1

    @staticmethod
    def is_valid_gtid_set(gtids):
        """
        Check that a GTID set, such as one published by another node, can be parsed
        """
        try:
            Mysql.execute_query_as_root("SELECT GTID_SUBSET(%s, '')", params=(gtids,))
            return True
        except mysql.connector.Error:
            return False

    @staticmethod
    def set_replication_unhealthy_flag(unhealthy):
        """
        Publish the replication health of this node to Consul if it has changed
        """
        consul = Consul.get_instance()

        if consul.replication_unhealthy != unhealthy:
            consul.node_set_replication_unhealthy_flag(unhealthy)

    @staticmethod
    def server_start(use_root_password=True, skip_config_build=False):
        """
        Start the MySQL server and wait for ready to serve connections.
        """

        logging.info("Starting MySQL")

        if not skip_config_build:
            Mysql.build_configuration()

        mysql_server = [Mysql.mysql_server_binary, "--user=mysql"]
        mysql_process = subprocess.Popen(mysql_server)

        # Use root password for the connection or not
        root_password = None
        if use_root_password:
            root_password = Utils.get_envvar_or_secret("MYSQL_ROOT_PASSWORD")

        Mysql.wait_for_connection(password=root_password)

        return mysql_process

    @staticmethod
    def server_stop():
        """
        Stop the MySQL server.
        """
        logging.info("Stopping MySQL Server")

        # Try to shutdown the server without a password
        result = Mysql.execute_statement(sql="SHUTDOWN", log_error=False)

        # Try to shutdown the server using the root password
        if not result:
            root_password = Utils.get_envvar_or_secret("MYSQL_ROOT_PASSWORD")
            Mysql.execute_statement(sql="SHUTDOWN", password=root_password)

    @staticmethod
    def execute_query_as_root(sql, database="mysql", discard_result=False, params=None):
        """
        Execute the SQL query and return result.
        """

        root_password = Utils.get_envvar_or_secret("MYSQL_ROOT_PASSWORD")

        cnx = None

        try:
            cnx = mysql.connector.connect(
                user="root",
                password=root_password,
                database=database,
                unix_socket="/var/run/mysqld/mysqld.sock",
            )

            cur = cnx.cursor(dictionary=True, buffered=True)
            cur.execute(sql, params)

            if discard_result:
                return None

            return cur.fetchall()
        finally:
            if cnx:
                cnx.close()

    @staticmethod
    def wait_for_connection(
        timeout=120, username="root", password=None, database="mysql"
    ):
        """
        Test connection via unix-socket. During first init
        MySQL start without network access.
        """
        elapsed_time = 0
        last_error = None

        while elapsed_time < timeout:
            try:
                cnx = mysql.connector.connect(
                    user=username,
                    password=password,
                    database=database,
                    unix_socket="/var/run/mysqld/mysqld.sock",
                )
                cnx.close()
                logging.debug("MySQL connection successfully")
                return True
            except mysql.connector.Error as err:
                time.sleep(1)
                elapsed_time = elapsed_time + 1
                last_error = err

        logging.error(
            "Unable to connect to MySQL (timeout=%i). %s", elapsed_time, last_error
        )
        sys.exit(1)

    @staticmethod
    def execute_statement_or_exit(
        sql=None, username="root", password=None, database="mysql", port=None, params=None
    ):
        """
        Execute the given SQL statement.
        """
        result = Mysql.execute_statement(
            sql=sql,
            username=username,
            port=port,
            password=password,
            database=database,
            params=params,
        )
        if not result:
            sys.exit(1)

    @staticmethod
    def execute_statement(
        sql=None,
        username="root",
        password=None,
        database="mysql",
        port=None,
        log_error=True,
        params=None,
    ):
        """
        Execute the given SQL statement.
        """
        try:
            if port is None:
                cnx = mysql.connector.connect(
                    user=username,
                    password=password,
                    database=database,
                    unix_socket="/var/run/mysqld/mysqld.sock",
                )

            else:
                cnx = mysql.connector.connect(
                    user=username, password=password, database=database, port=port
                )

            cursor = cnx.cursor()

            cursor.execute(sql, params)

            cnx.close()
            return True
        except mysql.connector.Error as err:
            if log_error:
                logging.error("Failed to execute SQL: %s", err)
            return False

    @staticmethod
    def create_backup_if_needed():
        """
        Create a new backup if needed. Default age is 15m
        """

        from mcm.snapshot import Snapshot

        logging.debug("Checking for backups")

        consul_client = Consul.get_instance()
        if consul_client.is_replication_leader():
            logging.debug(
                "We are the replication master, skipping backup check as snapshots run on replicas"
            )
            return False

        # A snapshotting replica cannot be promoted, so do not start a snapshot during a failover
        if consul_client.get_replication_leader_ip() is None:
            logging.debug("No replication leader available, skipping backup check")
            return False

        if Snapshot.isPending():
            logging.debug("A snapshot is already in progress, skipping")
            return False

        backup_date = Snapshot.getTime()
        maxage_seconds = int(Utils.get_envvar_or_secret("SNAPSHOT_MINUTES", 15)) * 60
        if maxage_seconds < 60:
            maxage_seconds = 60

        if Utils.is_refresh_needed(backup_date, timedelta(seconds=maxage_seconds)):
            logging.info("Snapshot is outdated (%s), creating new one", backup_date)

            # Perform backup in extra thread to prevent Consul loop interruption
            backup_thread = threading.Thread(target=Snapshot.create)
            backup_thread.start()

            return True

        return False

    @staticmethod
    def restore_backup_or_exit():
        """
        Restore a backup or exit
        """
        from mcm.snapshot import Snapshot

        result = Snapshot.restore()

        if not result:
            logging.error("Unable to restore MySQL backup")
            sys.exit(1)

    @staticmethod
    def check_replication_user_privileges():
        """
        Ensures that replication user privileges are still set correctly. This prevents the foot-gun
        of someone trying to manipulate this user.
        """

        # Ensure replication user is still available and set up correctly
        replication_user = Utils.get_envvar_or_secret("MYSQL_REPLICATION_USER")
        replication_password = Utils.get_envvar_or_secret("MYSQL_REPLICATION_PASSWORD")

        grants = Mysql.execute_query_as_root(
            "SHOW GRANTS FOR %s@%s", params=(replication_user, "%")
        )[0].get(f"Grants for {replication_user}@%", "")
        logging.debug(grants)

        if grants == "":
            logging.debug("Re-creating replication user")

            Mysql.execute_query_as_root(
                "CREATE USER %s@%s IDENTIFIED WITH caching_sha2_password BY %s",
                params=(replication_user, "%", replication_password),
            )
            Mysql.execute_query_as_root(
                "GRANT USAGE, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO %s@%s",
                params=(replication_user, "%"),
            )
            Mysql.execute_query_as_root("FLUSH PRIVILEGES")
        elif (
            grants
            != f"GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO {Mysql.quote_identifier(replication_user)}@`%`"
        ):
            logging.debug(
                "Deleting and re-creating replication user due to incorrect permissions"
            )

            Mysql.execute_query_as_root(
                "DROP USER %s@%s", params=(replication_user, "%")
            )
            Mysql.execute_query_as_root(
                "CREATE USER %s@%s IDENTIFIED WITH caching_sha2_password BY %s",
                params=(replication_user, "%", replication_password),
            )
            Mysql.execute_query_as_root(
                "GRANT USAGE, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO %s@%s",
                params=(replication_user, "%"),
            )
            Mysql.execute_query_as_root("FLUSH PRIVILEGES")

    @staticmethod
    def check_root_accounts():
        """
        Ensures that root can only connect from inside the container, or from the hosts defined by
        MYSQL_ROOT_HOST (comma separated). Remote root accounts for any other host are removed. Runs
        on the leader, as the accounts are replicated to the replicas.
        """
        local_hosts = ["localhost", "127.0.0.1", "::1"]
        root_hosts = [
            host.strip()
            for host in Utils.get_envvar_or_secret("MYSQL_ROOT_HOST", "").split(",")
            if host.strip() != "" and host.strip() not in local_hosts
        ]

        for account in Mysql.execute_query_as_root(
            "SELECT Host FROM mysql.user WHERE User = 'root'"
        ):
            if account["Host"] not in local_hosts and account["Host"] not in root_hosts:
                logging.info("Removing root account for host %s", account["Host"])
                Mysql.execute_query_as_root(
                    "DROP USER 'root'@%s", discard_result=True, params=(account["Host"],)
                )

        for root_host in root_hosts:
            logging.debug("Ensuring root account for host %s", root_host)
            Mysql.execute_query_as_root(
                "CREATE USER IF NOT EXISTS 'root'@%s IDENTIFIED WITH caching_sha2_password BY %s",
                discard_result=True,
                params=(root_host, Utils.get_envvar_or_secret("MYSQL_ROOT_PASSWORD")),
            )
            Mysql.execute_query_as_root(
                "GRANT ALL PRIVILEGES ON *.* TO 'root'@%s WITH GRANT OPTION",
                discard_result=True,
                params=(root_host,),
            )

    @staticmethod
    def quote_identifier(identifier):
        """
        Quote an identifier, such as a database name, for use in SQL. Identifiers cannot be passed
        as parameters.
        """
        return "`" + identifier.replace("`", "``") + "`"
