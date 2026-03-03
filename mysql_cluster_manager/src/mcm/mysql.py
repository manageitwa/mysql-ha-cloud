"""This file is part of the MySQL cluster manager"""

import logging
import os
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
    _replication_unhealthy_flag = False
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

        Mysql.execute_statement_or_exit(
            f"CREATE USER '{application_user}'@'%' "
            f"IDENTIFIED WITH caching_sha2_password BY '{appication_password}'"
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

        return True

    @staticmethod
    def build_configuration():
        """
        Build the MySQL server configuratuion.
        """
        consul = Consul.get_instance()
        server_id = consul.get_mysql_server_id()

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

        if (
            Utils.get_envvar_or_secret("MYSQL_TLS_REQUIRED", "True").lower() == "true"
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
                f"CHANGE REPLICATION SOURCE TO SOURCE_HOST = '{leader_ip}', "
                f"SOURCE_PORT = 3306, "
                "SOURCE_AUTO_POSITION = 1, GET_SOURCE_PUBLIC_KEY = 1, "
                f"SOURCE_SSL=1, SOURCE_SSL_CA = '{Utils.get_envvar('MYSQL_TLS_CA')}', "
                f"SOURCE_SSL_CERT = '{Utils.get_envvar('MYSQL_TLS_CERT')}', "
                f"SOURCE_SSL_KEY = '{Utils.get_envvar('MYSQL_TLS_KEY')}'",
                discard_result=True,
            )
        else:
            Mysql.execute_query_as_root(
                f"CHANGE REPLICATION SOURCE TO SOURCE_HOST = '{leader_ip}', "
                f"SOURCE_PORT = 3306, "
                "SOURCE_AUTO_POSITION = 1, GET_SOURCE_PUBLIC_KEY = 1",
                discard_result=True,
            )

        Mysql.execute_query_as_root(
            f"START REPLICA USER = '{replication_user}' "
            f"PASSWORD = '{replication_password}'",
            discard_result=True,
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

        slave_status = Mysql.execute_query_as_root("SHOW REPLICA STATUS")

        if len(slave_status) != 1:
            return False

        status = slave_status[0]

        # Check that the replication threads are running
        io_running = status.get("Replica_IO_Running", "No")
        sql_running = status.get("Replica_SQL_Running", "No")

        if io_running != "Yes" or sql_running != "Yes":
            io_error = status.get("Last_IO_Error", "")
            sql_error = status.get("Last_SQL_Error", "")

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
                f"START REPLICA USER = '{replication_user}' "
                f"PASSWORD = '{replication_password}'",
                discard_result=True,
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

            if not Mysql._replication_unhealthy_flag:
                Consul.get_instance().node_set_replication_unhealthy_flag(True)
                Mysql._replication_unhealthy_flag = True
            return False

        # Both threads are running — clear unhealthy flag and check if fully caught up
        if Mysql._replication_unhealthy_flag:
            Consul.get_instance().node_set_replication_unhealthy_flag(False)
            Mysql._replication_unhealthy_flag = False

        seconds_behind = status.get("Seconds_Behind_Source")
        lag_threshold = int(
            Utils.get_envvar_or_secret("MYSQL_REPLICATION_LAG_THRESHOLD", "5")
        )
        if lag_threshold > 0:
            if seconds_behind is not None and seconds_behind > lag_threshold:
                logging.warning("Replica is %s seconds behind source", seconds_behind)
                Consul.get_instance().node_set_replication_unhealthy_flag(True)
                Mysql._replication_lagging = True
            else:
                logging.debug("Replica is %s seconds behind source", seconds_behind)
                Consul.get_instance().node_set_replication_unhealthy_flag(False)
                Mysql._replication_lagging = False

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
    def execute_query_as_root(sql, database="mysql", discard_result=False):
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
            cur.execute(sql)

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
        sql=None, username="root", password=None, database="mysql", port=None
    ):
        """
        Execute the given SQL statement.
        """
        result = Mysql.execute_statement(
            sql=sql, username=username, port=port, password=password, database=database
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

            cursor.execute(sql)

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
            f"SHOW GRANTS FOR '{replication_user}'@'%'"
        )[0].get(f"Grants for {replication_user}@%", "")
        logging.debug(grants)

        if grants == "":
            logging.debug("Re-creating replication user")

            Mysql.execute_query_as_root(
                f"CREATE USER '{replication_user}'@'%' "
                f"IDENTIFIED WITH caching_sha2_password BY '{replication_password}'"
            )
            Mysql.execute_query_as_root(
                f"GRANT USAGE, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO '{replication_user}'@'%'"
            )
            Mysql.execute_query_as_root("FLUSH PRIVILEGES")
        elif (
            grants
            != f"GRANT USAGE, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO {replication_user}@%"
        ):
            logging.debug(
                "Deleting and re-creating replication user due to incorrect permissions"
            )

            Mysql.execute_query_as_root(f"DROP USER '{replication_user}'@'%'")
            Mysql.execute_query_as_root(
                f"CREATE USER '{replication_user}'@'%' "
                f"IDENTIFIED WITH caching_sha2_password BY '{replication_password}'"
            )
            Mysql.execute_query_as_root(
                f"GRANT USAGE, REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO '{replication_user}'@'%'"
            )
            Mysql.execute_query_as_root("FLUSH PRIVILEGES")
