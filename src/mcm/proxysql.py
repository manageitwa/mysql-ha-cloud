"""This file contains the ProxySQL related actions"""

import logging
import os
import subprocess
import sys
import threading
import time

import mysql.connector

from mcm.consul import Consul
from mcm.mysql import Mysql
from mcm.utils import Utils


class Proxysql:
    """
    This class encapsulates all ProxySQL related things
    """

    # The configuration database, saved to disk by ProxySQL
    database_file = "/var/lib/proxysql/proxysql.db"

    def __init__(self):
        """
        Init the instance
        """
        self.configured_mysql_servers = None
        self.server_update_thread = None

    @staticmethod
    def inital_setup():
        """
        Inital setup of ProxySQL
        """
        logging.info("Performing initial ProxySQL setup")

        # Change admin password if needed
        admin_password = Utils.get_envvar_or_secret("PROXYSQL_ADMIN_PASSWORD", "admin")

        # Credentials are separated by semicolons in the admin-admin_credentials variable
        if ";" in admin_password:
            logging.error("PROXYSQL_ADMIN_PASSWORD must not contain a semicolon")
            sys.exit(1)

        # Only allow access to the admin interface from inside the container
        Mysql.execute_statement_or_exit(
            sql="UPDATE global_variables SET variable_value='127.0.0.1:6032' "
            "WHERE variable_name='admin-mysql_ifaces'",
            username="admin",
            password="admin",
            database="",
            port=6032,
        )

        if admin_password != "admin":
            Mysql.execute_statement_or_exit(
                sql=f"UPDATE global_variables SET variable_value={Proxysql.quote('admin:' + admin_password)} "
                "WHERE variable_name='admin-admin_credentials'",
                username="admin",
                password="admin",
                database="",
                port=6032,
            )

        Mysql.execute_statement_or_exit(
            sql="LOAD ADMIN VARIABLES TO RUNTIME",
            username="admin",
            password="admin",
            database="",
            port=6032,
        )

        # Persist the admin variables, so the password is kept if ProxySQL is restarted
        Proxysql.perform_sql_query("SAVE ADMIN VARIABLES TO DISK")

        # Set up MySQL variables
        Proxysql.perform_sql_query(
            "UPDATE global_variables SET variable_value='caching_sha2_password' "
            "WHERE variable_name='mysql-default_authentication_plugin'"
        )
        Proxysql.perform_sql_query(
            f"UPDATE global_variables SET variable_value={Proxysql.quote(Utils.get_envvar('MYSQL_VERSION'))} "
            "WHERE variable_name='mysql-server_version'"
        )

        # Hold queries for up to 20 seconds while there is no available server in their hostgroup,
        # so that writes made during a failover wait for the new leader instead of failing
        Proxysql.perform_sql_query(
            "UPDATE global_variables SET variable_value='20000' "
            "WHERE variable_name='mysql-connect_timeout_server_max'"
        )

        # Setup Monitoring User
        replication_user = Utils.get_envvar_or_secret("MYSQL_REPLICATION_USER")
        replication_password = Utils.get_envvar_or_secret("MYSQL_REPLICATION_PASSWORD")

        Proxysql.perform_sql_query(
            f"UPDATE global_variables SET variable_value={Proxysql.quote(replication_user)} "
            "WHERE variable_name='mysql-monitor_username'"
        )
        Proxysql.perform_sql_query(
            f"UPDATE global_variables SET variable_value={Proxysql.quote(replication_password)} "
            "WHERE variable_name='mysql-monitor_password'"
        )

        # Enable TLS for MySQL backend connections if needed
        if (
            Utils.get_envvar("MYSQL_TLS_CA", False)
            and Utils.get_envvar("MYSQL_TLS_CERT", False)
            and Utils.get_envvar("MYSQL_TLS_KEY", False)
        ):
            Proxysql.perform_sql_query(
                f"UPDATE global_variables SET variable_value={Proxysql.quote(Utils.get_envvar('MYSQL_TLS_CA'))} "
                "WHERE variable_name='mysql-ssl_p2s_ca'"
            )
            Proxysql.perform_sql_query(
                f"UPDATE global_variables SET variable_value={Proxysql.quote(Utils.get_envvar('MYSQL_TLS_CERT'))} "
                "WHERE variable_name='mysql-ssl_p2s_cert'"
            )
            Proxysql.perform_sql_query(
                f"UPDATE global_variables SET variable_value={Proxysql.quote(Utils.get_envvar('MYSQL_TLS_KEY'))} "
                "WHERE variable_name='mysql-ssl_p2s_key'"
            )

        # Hostgroups (writer = 1, reader = 2) are assigned from the Consul replication leader in
        # set_mysql_server(), so ProxySQL must not move servers between them based on read_only
        Proxysql.perform_sql_query("DELETE FROM mysql_replication_hostgroups")

        # Configure read write split
        Proxysql.perform_sql_query("DELETE FROM mysql_query_rules")
        Proxysql.perform_sql_query(
            "INSERT INTO mysql_query_rules (active, match_digest, "
            "destination_hostgroup, apply) VALUES (1, '^SELECT.*', 2, 0)"
        )
        Proxysql.perform_sql_query(
            "INSERT INTO mysql_query_rules (active, match_digest, "
            "destination_hostgroup, apply) VALUES (1, '^SELECT.*FOR UPDATE', 1, 1)"
        )

        # Configure Application User
        application_user = Utils.get_envvar_or_secret("MYSQL_USER")
        application_password = Utils.get_envvar_or_secret("MYSQL_PASSWORD")

        # Force SSL/TLS for application connections if needed
        if (
            Utils.get_envvar("MYSQL_TLS_CA", False)
            and Utils.get_envvar("MYSQL_TLS_CERT", False)
            and Utils.get_envvar("MYSQL_TLS_KEY", False)
            and (
                Utils.get_envvar_or_secret("MYSQL_TLS_REQUIRED", "True").lower()
                == "true"
                or Utils.get_envvar_or_secret("MYSQL_TLS_REQUIRED", "True") == "1"
            )
        ):
            use_ssl = 1
        else:
            use_ssl = 0

        Proxysql.perform_sql_query("DELETE FROM mysql_users")
        Proxysql.perform_sql_query(
            "INSERT INTO mysql_users(username, password, use_ssl, default_hostgroup) "
            f"VALUES ({Proxysql.quote(application_user)}, {Proxysql.quote(application_password)}, {use_ssl}, 1)"
        )

        # Persist and activate config
        Proxysql.activate_config()

        # Copy TLS files to the right place for ProxySQL and initialise TLS
        if (
            Utils.get_envvar("MYSQL_TLS_CA", False)
            and Utils.get_envvar("MYSQL_TLS_CERT", False)
            and Utils.get_envvar("MYSQL_TLS_KEY", False)
        ):
            time.sleep(1)

            os.remove("/var/lib/proxysql/proxysql-ca.pem")
            os.remove("/var/lib/proxysql/proxysql-cert.pem")
            os.remove("/var/lib/proxysql/proxysql-key.pem")
            os.symlink(
                Utils.get_envvar("MYSQL_TLS_CA"), "/var/lib/proxysql/proxysql-ca.pem"
            )
            os.symlink(
                Utils.get_envvar("MYSQL_TLS_CERT"),
                "/var/lib/proxysql/proxysql-cert.pem",
            )
            os.symlink(
                Utils.get_envvar("MYSQL_TLS_KEY"), "/var/lib/proxysql/proxysql-key.pem"
            )

            Proxysql.perform_sql_query("PROXYSQL RELOAD TLS")

    @staticmethod
    def activate_config():
        """
        Persist and activate the ProxySQL configuration
        """
        Proxysql.perform_sql_query("LOAD MYSQL VARIABLES TO RUNTIME")
        Proxysql.perform_sql_query("LOAD MYSQL SERVERS TO RUNTIME")
        Proxysql.perform_sql_query("LOAD MYSQL USERS TO RUNTIME")
        Proxysql.perform_sql_query("LOAD MYSQL QUERY RULES TO RUNTIME")

        Proxysql.perform_sql_query("SAVE MYSQL VARIABLES TO DISK")
        Proxysql.perform_sql_query("SAVE MYSQL SERVERS TO DISK")
        Proxysql.perform_sql_query("SAVE MYSQL USERS TO DISK")
        Proxysql.perform_sql_query("SAVE MYSQL QUERY RULES TO DISK")

    @staticmethod
    def build_mysql_servers(mysql_nodes, leader_ip):
        """
        Build the backend MySQL server rows as (hostgroup, hostname, status, max_replication_lag).

        The leader is placed in the writer hostgroup and replicas are placed in the reader
        hostgroup. Nodes that are unavailable (restoring, snapshotting or unhealthy replication)
        are set to OFFLINE_SOFT so existing connections can finish, but no new connections are
        sent to them.
        """
        max_lag = int(
            Utils.get_envvar_or_secret("MYSQL_REPLICATION_LAG_THRESHOLD", "5")
        )

        mysql_servers = []

        for ip_address, available in mysql_nodes.items():
            status = "ONLINE" if available else "OFFLINE_SOFT"

            if ip_address == leader_ip:
                mysql_servers.append((1, ip_address, status, 0))
            else:
                mysql_servers.append((2, ip_address, status, max_lag))

        mysql_servers.sort()

        return mysql_servers

    @staticmethod
    def set_mysql_server(mysql_servers):
        """
        Set the backend MySQL server
        """
        logging.info("Removing all old backend MySQL Server")
        Proxysql.perform_sql_query("DELETE FROM mysql_servers")

        if (
            Utils.get_envvar("MYSQL_TLS_CA", False)
            and Utils.get_envvar("MYSQL_TLS_CERT", False)
            and Utils.get_envvar("MYSQL_TLS_KEY", False)
        ):
            use_ssl = 1
        else:
            use_ssl = 0

        for hostgroup, ip_address, status, max_lag in mysql_servers:
            logging.info(
                "Adding %s as backend MySQL Server (hostgroup=%s, status=%s)",
                ip_address,
                hostgroup,
                status,
            )

            Proxysql.perform_sql_query(
                "INSERT INTO mysql_servers(hostgroup_id, hostname, port, status, use_ssl, max_replication_lag) "
                f"VALUES ({int(hostgroup)}, {Proxysql.quote(ip_address)}, 3306, {Proxysql.quote(status)}, "
                f"{use_ssl}, {int(max_lag)})"
            )

        Proxysql.perform_sql_query("LOAD MYSQL SERVERS TO RUNTIME")
        Proxysql.perform_sql_query("SAVE MYSQL SERVERS TO DISK")

    def update_mysql_server_if_needed(self, mysql_nodes, leader_ip):
        """
        Update the MySQL-Servers if needed (changed)
        """
        mysql_servers = Proxysql.build_mysql_servers(mysql_nodes, leader_ip)

        if self.configured_mysql_servers != mysql_servers:
            logging.info(
                "MySQL backend has changed (old=%s, new=%s), reconfiguring",
                self.configured_mysql_servers,
                mysql_servers,
            )
            Proxysql.set_mysql_server(mysql_servers)
            self.configured_mysql_servers = mysql_servers
            return True

        return False

    def start_server_update_thread(self):
        """
        Start the thread that keeps the backend MySQL servers up to date with the cluster. This
        runs for the life of the node, including while it is still restoring and starting MySQL,
        so that queries can be routed to the other nodes.
        """
        logging.info("Starting the ProxySQL server update thread")
        self.server_update_thread = threading.Thread(
            target=self.update_mysql_servers, args=(), daemon=True
        )
        self.server_update_thread.start()

    def update_mysql_servers(self):
        """
        Update the backend MySQL servers from Consul every couple of seconds
        """
        while True:
            try:
                # The leader is read before the node states, so that a newly promoted leader is
                # seen with its state published before its promotion
                consul = Consul.get_instance()
                leader_ip = consul.get_replication_leader_ip()
                self.update_mysql_server_if_needed(
                    consul.get_registered_node_states(), leader_ip
                )
            except (Exception, SystemExit):
                # Failed queries exit through SystemExit, which must not end the thread
                logging.exception("Unable to update the ProxySQL backend servers")

            time.sleep(2)

    @staticmethod
    def is_configured():
        """
        Check that ProxySQL is running and has the application user loaded, so it can accept
        queries
        """
        cnx = None

        try:
            cnx = mysql.connector.connect(
                user="admin",
                password=Utils.get_envvar_or_secret("PROXYSQL_ADMIN_PASSWORD", "admin"),
                port=6032,
                connection_timeout=3,
            )

            cursor = cnx.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM runtime_mysql_users WHERE username = %s",
                (Utils.get_envvar_or_secret("MYSQL_USER"),),
            )

            # The ProxySQL admin interface returns all values as strings
            return int(cursor.fetchone()[0]) > 0
        except mysql.connector.Error as err:
            logging.error("Unable to query ProxySQL: %s", err)
            return False
        finally:
            if cnx:
                cnx.close()

    @staticmethod
    def quote(value):
        """
        Quote a value for use in SQL on the ProxySQL admin interface. Parameters cannot be used, as
        the connector queries the SQL mode to escape them, which the admin interface does not
        support. The admin interface is backed by SQLite, which escapes quotes by doubling them.
        """
        return "'" + str(value).replace("'", "''") + "'"

    @staticmethod
    def perform_sql_query(sql):
        """
        Perform a SQL query
        """
        Mysql.execute_statement_or_exit(
            sql=sql,
            username="admin",
            password=Utils.get_envvar_or_secret("PROXYSQL_ADMIN_PASSWORD", "admin"),
            database="",
            port=6032,
        )

    @staticmethod
    def start_proxysql():
        """
        Start the ProxySQL
        """

        # Start from a clean configuration, which inital_setup() then applies and saves to disk.
        # --initial is not used for this, as ProxySQL's watchdog restarts a crashed ProxySQL with the
        # same arguments, which would discard the saved configuration.
        if os.path.exists(Proxysql.database_file):
            os.remove(Proxysql.database_file)

        # Init proxysql
        proxysql_init = [
            "/usr/bin/proxysql",
            "--idle-threads",
            "-c",
            "/etc/proxysql.cnf",
        ]
        subprocess.run(proxysql_init, check=True)

        # ProxySQL starts as a daemon, so wait for the admin interface to be available. The initial
        # admin credentials are used, as inital_setup() changes the password afterwards.
        for _ in range(30):
            try:
                cnx = mysql.connector.connect(
                    user="admin", password="admin", port=6032, connection_timeout=3
                )
                cnx.close()
                return True
            except mysql.connector.Error:
                time.sleep(1)

        logging.error("ProxySQL admin interface did not become available, exiting")
        sys.exit(1)
