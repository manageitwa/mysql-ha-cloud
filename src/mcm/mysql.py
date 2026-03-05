"""This module contains the Mysql class for the MCM"""

import logging
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import cast

import mysql.connector
from mysql.connector.types import MySQLConvertibleType, RowItemType

from .constants import DATA_DIR, MYSQLD_PATH, SOCKET_PATH
from .consul import Consul
from .utils import Utils


class Mysql:
    """
    Mysql class.

    Handles the MySQL connection and (statically) the server.
    """

    _username: str
    _password: str | None = None
    _database: str | None = None
    _use_socket: bool = False
    _replication_unhealthy: bool = False
    _replication_lagging: bool = False
    _server_process: subprocess.Popen[bytes] | None = None

    def __init__(
        self,
        username: str,
        password: str | None = None,
        database: str | None = None,
        use_socket: bool = False,
    ):
        self._username = username
        self._password = password
        self._database = database
        self._use_socket = use_socket

    def execute_statement(
        self,
        sql: str,
        params: Sequence[MySQLConvertibleType] | dict[str, MySQLConvertibleType] = (),
        return_result: bool = False,
        log_on_error: bool = True,
        exit_on_error: bool = False,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
    ) -> list[dict[str, RowItemType]] | None:
        """
        Execute the given SQL statement, optionally with parameters.
        """
        try:
            conn = mysql.connector.connect(
                user=username if username else self._username,
                password=password if password else self._password,
                database=database if database else self._database,
                unix_socket=SOCKET_PATH if self._use_socket else None,
            )

            cursor = conn.cursor(dictionary=True, prepared=len(params) != 0)
            _ = cursor.execute(sql, params)

            try:
                if return_result:
                    results = cast(list[dict[str, RowItemType]], cursor.fetchall())
                    return results

                return
            finally:
                _ = cursor.close()
                conn.close()
        except mysql.connector.Error as err:
            if log_on_error:
                logging.error("Failed to execute SQL: %s", err)

            if exit_on_error:
                sys.exit(1)

    def execute_statement_as_root(
        self,
        sql: str,
        params: Sequence[MySQLConvertibleType] | dict[str, MySQLConvertibleType] = (),
        return_result: bool = True,
        log_on_error: bool = True,
        exit_on_error: bool = False,
        database: str | None = None,
    ) -> list[dict[str, RowItemType]] | None:
        """
        Execute the SQL query as the root user, optionally with parameters.
        """

        return self.execute_statement(
            sql=sql,
            params=params,
            return_result=return_result,
            log_on_error=log_on_error,
            exit_on_error=exit_on_error,
            username="root",
            password=Utils.get_envvar_or_secret("MYSQL_ROOT_PASSWORD"),
            database=database,
        )

    def execute_statement_or_exit(
        self,
        sql: str,
        params: Sequence[MySQLConvertibleType] | dict[str, MySQLConvertibleType] = (),
        return_result: bool = False,
        log_on_error: bool = True,
        username: str | None = None,
        password: str | None = None,
        database: str | None = None,
    ) -> list[dict[str, RowItemType]] | None:
        """
        Execute the given SQL statement, exiting if the SQL statement fails.
        """
        return self.execute_statement(
            sql=sql,
            params=params,
            return_result=return_result,
            log_on_error=log_on_error,
            exit_on_error=True,
            username=username,
            password=password,
            database=database,
        )

    def make_read_only_replica(self, leader_ip: str) -> None:
        """
        Changes this instance to a read-only replica, using the given leader IP address as the source.
        """

        logging.info("Setting up replication (leader=%s)", leader_ip)

        use_ssl = (
            Utils.get_envvar("MYSQL_TLS_CA", "0") != "0"
            and Utils.get_envvar("MYSQL_TLS_CERT", "0") != "0"
            and Utils.get_envvar("MYSQL_TLS_KEY", "0") != "0"
        )

        # Stop replication
        _ = self.execute_statement_as_root("STOP REPLICA", return_result=False)

        # Change replication source
        _ = self.execute_statement_as_root(
            """
                CHANGE REPLICATION SOURCE TO SOURCE_HOST = %(host)s,
                SOURCE_PORT = 3306,
                SOURCE_AUTO_POSITION = 1,
                GET_SOURCE_PUBLIC_KEY = 1,
                SOURCE_SSL = 1,
                SOURCE_SSL_CA = %(tls_ca)s,
                SOURCE_SSL_CERT = %(tls_cert)s,
                SOURCE_SSL_KEY = %(tls_key)s
            """
            if use_ssl
            else """
                CHANGE REPLICATION SOURCE TO SOURCE_HOST = %(host)s,
                SOURCE_PORT = 3306,
                SOURCE_AUTO_POSITION = 1,
                GET_SOURCE_PUBLIC_KEY = 1
            """,
            {
                "host": leader_ip,
                "tls_ca": Utils.get_envvar("MYSQL_TLS_CA") if use_ssl else None,
                "tls_cert": Utils.get_envvar("MYSQL_TLS_CERT") if use_ssl else None,
                "tls_key": Utils.get_envvar("MYSQL_TLS_KEY") if use_ssl else None,
            },
            return_result=False,
        )

        # Start up the replication and set this instance to read-only
        _ = self.execute_statement_as_root(
            """
            START REPLICA USER = %(user)s
            PASSWORD = %(password)s
            """,
            {
                "user": Utils.get_envvar_or_secret("MYSQL_REPLICATION_USER"),
                "password": Utils.get_envvar_or_secret("MYSQL_REPLICATION_PASSWORD"),
            },
            return_result=False,
        )

        logging.info("Started replication, set MySQL to read-only")
        _ = self.execute_statement_as_root(
            "SET GLOBAL read_only = 1", return_result=False
        )
        _ = self.execute_statement_as_root(
            "SET GLOBAL super_read_only = 1", return_result=False
        )

    def make_primary_instance(self) -> None:
        """
        Stop the replication
        """
        logging.info("Stopping replication")
        _ = self.execute_statement_as_root("STOP REPLICA", return_result=False)
        _ = self.execute_statement_as_root("RESET REPLICA ALL", return_result=False)

        # Accept writes
        logging.info("Stopped replication, set MySQL to read-write")
        _ = self.execute_statement_as_root(
            "SET GLOBAL super_read_only = 0", return_result=False
        )
        _ = self.execute_statement_as_root(
            "SET GLOBAL read_only = 0", return_result=False
        )

    def get_replication_source_ip(self) -> str | None:
        """
        Get the current replication leader ip
        """
        replica_status = self.execute_statement_as_root(
            "GET REPLICA STATUS", return_result=False
        )

        if replica_status is None:
            return None

        if len(replica_status) != 1:
            return None

        status = replica_status[0]

        if "Source_Host" not in status:
            logging.error("Invalid output, Source_Host not found %s", replica_status)
            return None

        return cast(str, status.get("Source_Host", ""))

    def is_replication_healthy(self) -> bool:
        """
        Check that replication is running and all data from the leader
        has been applied locally. If a replication thread has stopped,
        attempt an automatic restart.

        Returns True only when both IO and SQL threads are running
        AND the replica is fully caught up with the leader.
        """

        from .snapshot import Snapshot

        if Snapshot.is_snapshotting:
            logging.debug("Skipping replication health check during snapshot")
            return True

        replica_status = self.execute_statement_as_root("SHOW REPLICA STATUS")

        if replica_status is None:
            return False

        if len(replica_status) != 1:
            return False

        status = replica_status[0]

        # Check that the replication threads are running
        io_running = status.get("Replica_IO_Running", "No")
        sql_running = status.get("Replica_SQL_Running", "No")

        if io_running != "Yes" or sql_running != "Yes":
            io_error = status.get("Last_IO_Error", "")
            sql_error = status.get("Last_SQL_Error", "")

            logging.warning(
                "Replication is not healthy (IO_Running=%s, SQL_Running=%s, "
                + "IO_Error='%s', SQL_Error='%s'), attempting restart",
                io_running,
                sql_running,
                io_error,
                sql_error,
            )

            _ = self.execute_statement_as_root("STOP REPLICA", return_result=False)
            _ = self.execute_statement_as_root(
                """
                START REPLICA USER = %(user)s
                PASSWORD = %(password)s
                """,
                {
                    "user": Utils.get_envvar_or_secret("MYSQL_REPLICATION_USER"),
                    "password": Utils.get_envvar_or_secret(
                        "MYSQL_REPLICATION_PASSWORD"
                    ),
                },
                return_result=False,
            )

            # Allow threads time to start before verifying
            time.sleep(5)

            verify_status = self.execute_statement_as_root("SHOW REPLICA STATUS")

            if verify_status is not None and len(verify_status) == 1:
                new_io = verify_status[0].get("Replica_IO_Running", "No")
                new_sql = verify_status[0].get("Replica_SQL_Running", "No")

                if new_io == "No" or new_sql == "No":
                    logging.error(
                        "Replication restart failed (IO_Running=%s, SQL_Running=%s, "
                        + "IO_Error='%s', SQL_Error='%s')",
                        new_io,
                        new_sql,
                        verify_status[0].get("Last_IO_Error", ""),
                        verify_status[0].get("Last_SQL_Error", ""),
                    )
                else:
                    logging.info("Replication restart succeeded")

            if not self._replication_unhealthy:
                Consul.get_instance().node_set_replication_unhealthy_flag(True)
                self._replication_unhealthy = True
            return False

        # Both threads are running — clear unhealthy flag and check if fully caught up
        if self._replication_unhealthy:
            Consul.get_instance().node_set_replication_unhealthy_flag(False)
            self._replication_unhealthy = False

        seconds_behind = cast(int | None, status.get("Seconds_Behind_Source"))
        lag_threshold = int(
            Utils.get_envvar_or_secret("MYSQL_REPLICATION_LAG_THRESHOLD", "5")
        )

        if lag_threshold > 0:
            if seconds_behind is not None and seconds_behind > lag_threshold:
                logging.warning("Replica is %s seconds behind source", seconds_behind)
                Consul.get_instance().node_set_replication_unhealthy_flag(True)
                self._replication_lagging = True
            else:
                logging.debug("Replica is %s seconds behind source", seconds_behind)
                Consul.get_instance().node_set_replication_unhealthy_flag(False)
                self._replication_lagging = False

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
    def server_start(
        use_root_password: bool = True, skip_config_build: bool = False
    ) -> subprocess.Popen[bytes]:
        """
        Start the MySQL server on a new thread and wait for it to be ready to serve
        connections.
        """

        if Mysql._server_process:
            return Mysql._server_process

        logging.info("Starting MySQL")

        if not skip_config_build:
            Mysql.build_configuration()

        Mysql._server_process = subprocess.Popen([MYSQLD_PATH, "--user=mysql"])
        Mysql.wait_for_connection(use_root_password)

        return Mysql._server_process

    @staticmethod
    def server_process() -> subprocess.Popen[bytes] | None:
        """
        Gets the current MySQL server process if available
        """
        return Mysql._server_process

    @staticmethod
    def server_stop() -> None:
        """
        Stop the MySQL server if it is currently active
        """

        if not Mysql._server_process:
            return

        logging.info("Stopping MySQL Server")

        # Try to shutdown the server
        instance = Mysql(
            "root", Utils.get_envvar_or_secret("MYSQL_ROOT_PASSWORD"), use_socket=True
        )
        _ = instance.execute_statement_as_root("SHUTDOWN", return_result=False)

        _ = Mysql._server_process.wait()
        Mysql._server_process = None

    @staticmethod
    def wait_for_connection(
        timeout: int = 120,
        use_root_password: bool = True,
    ) -> None:
        """
        Test connection via unix-socket. During first init
        MySQL start without network access.
        """
        elapsed_time = 0
        last_error = None

        while elapsed_time < timeout:
            try:
                conn = mysql.connector.connect(
                    user="root",
                    password=Utils.get_envvar_or_secret("MYSQL_ROOT_PASSWORD")
                    if use_root_password
                    else None,
                    database=None,
                    unix_socket=SOCKET_PATH,
                )
                conn.close()
                logging.debug("MySQL connection successfully")
            except mysql.connector.Error as err:
                time.sleep(1)
                elapsed_time = elapsed_time + 1
                last_error = err

        logging.error(
            "Unable to connect to MySQL (timeout=%i). %s", elapsed_time, last_error
        )
        sys.exit(1)

    @staticmethod
    def initialize_database() -> None:
        """
        Initializes the MySQL database, creates the users and configures permissions.
        """

        logging.info("Init MySQL database directory")

        if os.path.isfile(f"{DATA_DIR}/ib_logfile0"):
            logging.info("MySQL is already initialized, skipping")
            return

        # Create directory
        mysql_init = [MYSQLD_PATH, "--initialize-insecure", "--user=mysql"]
        result = subprocess.run(mysql_init, check=True)

        if result.returncode != 0:
            raise Exception(f"Unable to initialize MySQL data directory at {DATA_DIR}")

        # Start server the first time
        mysql_process = Mysql.server_start(
            use_root_password=False, skip_config_build=True
        )

        # Create connection instance
        instance = Mysql("root")

        # Create application user
        logging.debug("Creating MySQL user for the application")
        application_user = Utils.get_envvar_or_secret("MYSQL_USER")
        application_password = Utils.get_envvar_or_secret("MYSQL_PASSWORD")

        Mysql.execute_statement_or_exit(
            f"CREATE USER '{application_user}'@'%' "
            f"IDENTIFIED WITH caching_sha2_password BY '{application_password}'"
        )

        # Create backup user
        logging.debug("Creating MySQL user for backups")
        backup_user = Utils.get_envvar_or_secret("MYSQL_BACKUP_USER")
        backup_password = Utils.get_envvar_or_secret("MYSQL_BACKUP_PASSWORD")
        Mysql.execute_statement_or_exit(
            f"CREATE USER '{backup_user}'@'localhost' "
            f"IDENTIFIED WITH caching_sha2_password BY '{backup_password}'"
        )
        Mysql.execute_statement_or_exit(
            "GRANT BACKUP_ADMIN, PROCESS, RELOAD, LOCK TABLES, REPLICATION CLIENT, REPLICATION_SLAVE_ADMIN, "
            f"REPLICATION CLIENT ON *.* TO '{backup_user}'@'localhost'"
        )
        Mysql.execute_statement_or_exit(
            "GRANT SELECT ON performance_schema.log_status TO "
            f"'{backup_user}'@'localhost'"
        )
        Mysql.execute_statement_or_exit(
            "GRANT SELECT ON performance_schema.keyring_component_status TO "
            f"'{backup_user}'@'localhost'"
        )
        Mysql.execute_statement_or_exit(
            "GRANT SELECT ON performance_schema.replication_group_members TO "
            f"'{backup_user}'@'localhost'"
        )

        # Create replication user
        logging.debug("Creating replication user")
        replication_user = Utils.get_envvar_or_secret("MYSQL_REPLICATION_USER")
        replication_password = Utils.get_envvar_or_secret("MYSQL_REPLICATION_PASSWORD")
        Mysql.execute_statement_or_exit(
            f"CREATE USER '{replication_user}'@'%' "
            f"IDENTIFIED WITH caching_sha2_password BY '{replication_password}'"
        )
        Mysql.execute_statement_or_exit(
            f"GRANT USAGE, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO '{replication_user}'@'%'"
        )

        # Change permissions for the root user
        logging.debug("Set permissions for the root user")
        root_password = Utils.get_envvar_or_secret("MYSQL_ROOT_PASSWORD")
        Mysql.execute_statement_or_exit(
            f"CREATE USER 'root'@'%' IDENTIFIED WITH caching_sha2_password BY '{root_password}'"
        )
        Mysql.execute_statement_or_exit(
            "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' WITH GRANT OPTION"
        )
        Mysql.execute_statement_or_exit(
            "ALTER USER 'root'@'localhost' "
            f"IDENTIFIED WITH caching_sha2_password BY '{root_password}'"
        )

        # Create database if specified
        if Utils.get_envvar_or_secret("MYSQL_DATABASE"):
            database_name = Utils.get_envvar_or_secret("MYSQL_DATABASE")
            logging.debug("Setting up initial database")
            Mysql.execute_statement_or_exit(
                sql=f"CREATE DATABASE IF NOT EXISTS `{database_name}`",
                username="root",
                password=root_password,
            )
            Mysql.execute_statement_or_exit(
                sql=f"GRANT ALL PRIVILEGES ON `{database_name}`.* TO '{application_user}'@'%'",
                username="root",
                password=root_password,
            )

        # Shutdown MySQL server
        logging.debug("Inital MySQL setup done, shutdown server..")
        Mysql.execute_statement_or_exit(
            sql="SHUTDOWN", username="root", password=root_password
        )
        mysql_process.wait()

    @staticmethod
    def build_configuration() -> None:
        """
        Build the MySQL server configuration.
        """
        consul = Consul.get_instance()
        server_id = consul.get_mysql_server_id()

        config = open("/etc/mysql/conf.d/zz-cluster.cnf", "w")
        config.writelines(
            [
                "# DO NOT EDIT - This file was generated automatically by MCM\n",
                "\n",
                "[mysqld]\n",
                f"server_id={server_id}\n",
                "gtid_mode=ON\n",
                "enforce-gtid-consistency=ON\n",
                "binlog_expire_logs_auto_purge=ON\n",
                "binlog_cache_size=5242880\n",
                f"binlog_expire_logs_seconds={int(Utils.get_envvar_or_secret('SNAPSHOT_MINUTES', '15')) * 60}\n",
            ]
        )

        if (
            Utils.get_envvar("MYSQL_TLS_CA", "0") != "0"
            and Utils.get_envvar("MYSQL_TLS_CERT", "0") != "0"
            and Utils.get_envvar("MYSQL_TLS_KEY", "0") != "0"
        ):
            config.writelines(
                [
                    f"ssl_ca={Utils.get_envvar('MYSQL_TLS_CA')}\n",
                    f"ssl_cert={Utils.get_envvar('MYSQL_TLS_CERT')}\n",
                    f"ssl_key={Utils.get_envvar('MYSQL_TLS_KEY')}\n",
                ]
            )

        if (
            Utils.get_envvar_or_secret("MYSQL_TLS_REQUIRED", "True").lower() == "true"
            or Utils.get_envvar_or_secret("MYSQL_TLS_REQUIRED", "True") == "1"
        ):
            config.writelines(
                [
                    "require_secure_transport=ON\n",
                ]
            )

        config.close()

    @staticmethod
    def restore_backup_or_exit() -> None:
        """
        Restore a backup or exit
        """
        from .snapshot import Snapshot

        result = Snapshot.restore()

        if not result:
            logging.error("Unable to restore MySQL backup")
            sys.exit(1)

    @staticmethod
    def check_replication_user_privileges() -> None:
        """
        Ensures that replication user privileges are still set correctly. This prevents the foot-gun
        of someone trying to manipulate this user.
        """

        # Ensure replication user is still available and set up correctly
        replication_user = Utils.get_envvar_or_secret("MYSQL_REPLICATION_USER")
        replication_password = Utils.get_envvar_or_secret("MYSQL_REPLICATION_PASSWORD")

        grants = Mysql.execute_statement_as_root(
            f"SHOW GRANTS FOR '{replication_user}'@'%'"
        )[0].get(f"Grants for {replication_user}@%", "")
        logging.debug(grants)

        if grants == "":
            logging.debug("Re-creating replication user")

            Mysql.execute_statement_as_root(
                f"CREATE USER '{replication_user}'@'%' "
                f"IDENTIFIED WITH caching_sha2_password BY '{replication_password}'"
            )
            Mysql.execute_statement_as_root(
                f"GRANT USAGE, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO '{replication_user}'@'%'"
            )
            Mysql.execute_statement_as_root("FLUSH PRIVILEGES")
        elif (
            grants
            != f"GRANT USAGE, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO {replication_user}@%"
        ):
            logging.debug(
                "Deleting and re-creating replication user due to incorrect permissions"
            )

            Mysql.execute_statement_as_root(f"DROP USER '{replication_user}'@'%'")
            Mysql.execute_statement_as_root(
                f"CREATE USER '{replication_user}'@'%' "
                f"IDENTIFIED WITH caching_sha2_password BY '{replication_password}'"
            )
            Mysql.execute_statement_as_root(
                f"GRANT USAGE, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO '{replication_user}'@'%'"
            )
            Mysql.execute_statement_as_root("FLUSH PRIVILEGES")
