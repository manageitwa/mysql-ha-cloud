"""This file is part of the MySQL cluster manager"""

import json
import logging
import socket
import subprocess
import sys
import threading
import time

import consul as pyconsul
import netifaces

from mcm.utils import Utils


class Consul:
    """
    This class encapsulates all Consul related things
    """

    # The signeton instance
    __instance = None

    # KV prefix
    kv_prefix = "mcm/"

    # Server ID key
    kv_server_id = kv_prefix + "server_id"

    # Instances ID key
    instances_path = kv_prefix + "instances/"

    # Instances session key
    instances_session_key = kv_prefix + "instances"

    # Replication leader path
    replication_leader_path = kv_prefix + "replication_leader"

    def __init__(self):
        """
        Init the Consul client
        """
        if Consul.__instance is not None:
            raise Exception("This class is a singleton!")

        Consul.__instance = self
        logging.info("Register Consul connection")

        # Allow 30 seconds for Consul agent to start
        for _ in range(6):
            try:
                self.client = pyconsul.Consul()
            except:
                logging.warning("Unable to connect to Consul, retrying in 5 seconds")
                time.sleep(5)
                continue

        if not self.client:
            raise Exception("Unable to establish a connection with Consul")

        self.node_health_session = None
        self.create_node_health_session()
        self.mysql_version = None
        self.server_id = None

        # The session auto refresh thread
        self.auto_refresh_thread = None
        self.run_auto_refresh_thread = False

    @staticmethod
    def get_instance():
        """Static access method."""
        if Consul.__instance is None:
            return Consul()

        return Consul.__instance

    def start_session_auto_refresh_thread(self):
        """
        Start the session auto refresh thread
        """
        logging.info("Starting the Consul session auto refresh thread")
        self.run_auto_refresh_thread = True
        self.auto_refresh_thread = threading.Thread(
            target=self.auto_refresh_sessions, args=()
        )
        self.auto_refresh_thread.start()

    def auto_refresh_sessions(self):
        """
        Auto refresh the active sessions
        """
        while self.run_auto_refresh_thread:
            logging.debug("Refreshing active consul sessions from auto refresh thread")
            self.refresh_sessions()
            time.sleep(5)

    def stop_session_auto_refresh_thread(self):
        """
        Stop the session auto refresh thread
        """
        logging.info("Stopping the Consul session auto refresh thread")
        self.run_auto_refresh_thread = False
        if self.auto_refresh_thread is not None:
            self.auto_refresh_thread.join()
            self.auto_refresh_thread = None
        logging.info("Consul session auto refresh thread is stopped")

    def create_node_health_session(self):
        """
        Create the node health session
        all created KV entries automatically removed
        on session destory.
        """

        session = None

        # Allow 30 seconds for session to be created
        for _ in range(6):
            try:
                self.node_health_session = self.client.session.create(
                    name=Consul.instances_session_key,
                    behavior="delete",
                    ttl=15,
                    lock_delay=0,
                )

                return self.node_health_session
            except:
                logging.warning(
                    "Unable to create a session in Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        if session is None:
            raise Exception("Unable to create node health session")

    def get_all_registered_nodes(self):
        """
        Get all registered MySQL nodes
        """
        mysql_nodes = []

        # Allow 3 minutes of retries to get the nodes as this will usually only fail on a potential
        # network downtime
        for _ in range(36):
            try:
                result = self.client.kv.get(Consul.instances_path, recurse=True)

                if result[1] is not None:
                    for node in result[1]:
                        node_value = node["Value"]
                        node_data = json.loads(node_value)

                        if not "ip_address" in node_data:
                            logging.error("ip_address missing in %s", node)
                            continue

                        if "restoring" in node_data and node_data["restoring"] is True:
                            logging.debug(
                                "Skipping node %s as it is currently restoring",
                                node_data,
                            )
                            continue

                        if (
                            "snapshotting" in node_data
                            and node_data["snapshotting"] is True
                        ):
                            logging.debug(
                                "Skipping node %s as it is currently snapshotting",
                                node_data,
                            )
                            continue

                        ip_address = node_data["ip_address"]
                        mysql_nodes.append(ip_address)

                return mysql_nodes
            except:
                logging.warning(
                    "Unable to get registered nodes from Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        return mysql_nodes

    def get_mysql_server_id(self):
        """
        Get the MySQL server id from consul

        Try to get existing value and update to +1
          * If Update fails, retry
          * If Key not exists, try to create
        """

        # Allow 30 seconds for a server ID to be assigned
        for _ in range(6):
            try:
                result = self.client.kv.get(Consul.kv_server_id)

                # Create new key
                if result[1] is None:
                    logging.debug(
                        "Old serverkey %s not found, preparing new one",
                        Consul.kv_server_id,
                    )

                    json_string = json.dumps({"last_used_id": 1})

                    # Try to create
                    put_result = self.client.kv.put(
                        Consul.kv_server_id, json_string, cas=0
                    )
                    if put_result is True:
                        logging.debug("Created new key, started new server counter")
                        return 1

                    logging.debug("New key could not be created, retrying")
                    continue

                # Updating existing key
                logging.debug("Updating existing key %s", result)
                json_string = result[1]["Value"]
                version = result[1]["ModifyIndex"]
                server_data = json.loads(json_string)

                if not "last_used_id" in server_data:
                    logging.error(
                        "Invalid JSON returned (missing last_used_id) %s", json_string
                    )

                server_data["last_used_id"] = server_data["last_used_id"] + 1
                json_string = json.dumps(server_data)
                put_result = self.client.kv.put(
                    Consul.kv_server_id, json_string, cas=version
                )

                if put_result is True:
                    logging.debug(
                        "Successfully updated consul value %s, new server_id is %i",
                        put_result,
                        server_data["last_used_id"],
                    )
                    return server_data["last_used_id"]
            except:
                logging.debug("Unable to get MYSQL server ID, retrying in 5 seconds")
                time.sleep(5)

        raise Exception("Unable to determine server id")

    def is_replication_leader(self):
        """
        Test if this is the MySQL replication leader or not
        """

        # Allow 3 minutes of retries
        for _ in range(36):
            try:
                result = self.client.kv.get(Consul.replication_leader_path)

                if result[1] is None:
                    logging.debug("No replication leader node available")
                    return False

                leader_session = result[1]["Session"]

                logging.debug(
                    "Replication leader is %s, we are %s",
                    leader_session,
                    self.node_health_session,
                )

                return leader_session == self.node_health_session
            except:
                logging.warning(
                    "Unable to determine replication leader from Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        return False

    def get_replication_leader_ip(self):
        """
        Get the IP of the current replication ledear
        """

        # Allow 3 minutes of retries
        for _ in range(36):
            try:
                result = self.client.kv.get(Consul.replication_leader_path)

                if result[1] is None:
                    return None

                json_string = result[1]["Value"]
                server_data = json.loads(json_string)

                if not "ip_address" in server_data:
                    logging.error(
                        "Invalid JSON returned from replication ledader (missing server_id) %s",
                        json_string,
                    )

                return server_data["ip_address"]
            except:
                logging.warning(
                    "Unable to get replication leader IP from Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        return None

    def try_to_become_replication_leader(self):
        """
        Try to become the new replication leader
        """

        # Allow 3 minutes of retries
        for _ in range(36):
            try:
                result = self.client.kv.get(Consul.replication_leader_path)

                if result[1] is None:
                    logging.debug("Register MySQL instance in Consul")
                    ip_address = Consul.getLocalIp()

                    json_string = json.dumps({"ip_address": ip_address})

                    put_result = self.client.kv.put(
                        Consul.replication_leader_path,
                        json_string,
                        acquire=self.node_health_session,
                    )

                    if put_result:
                        logging.info("We are the new replication leader")
                    else:
                        logging.debug("Unable to become replication leader, retry")

                    return put_result

                return False
            except:
                logging.warning(
                    "Unable to become replication leader due to error communicating with Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        return False

    def register_service(self, leader=False, port=3306):
        """
        Register the MySQL primary service
        """

        # Allow 30 seconds for session to be created
        for _ in range(6):
            try:
                ip_address = Consul.getLocalIp()

                tags = []
                service_id = f"mysql_{ip_address}"

                if leader:
                    tags.append("leader")
                else:
                    tags.append("follower")

                # Unrregister old service
                all_services = self.client.agent.services()

                if service_id in all_services:
                    logging.debug(
                        "Unregister old service %s (%s)", service_id, all_services
                    )
                    self.client.agent.service.deregister(service_id)

                # Register new service
                logging.info("Register new service_id=%s, tags=%s", service_id, tags)
                self.client.agent.service.register(
                    "mysql", service_id=service_id, port=port, tags=tags
                )

                return True
            except:
                logging.warning(
                    "Unable to register service in Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        return False

    def register_node(self):
        """
        Register the node in Consul
        """

        # Allow 30 seconds for node to be registered
        for _ in range(6):
            try:
                logging.debug("Register MySQL instance in Consul")
                ip_address = Consul.getLocalIp()

                json_string = json.dumps(
                    {
                        "ip_address": ip_address,
                        "server_id": "",
                        "mysql_version": "",
                        "snapshotting": False,
                        "restoring": False,
                    }
                )

                path = f"{Consul.instances_path}{ip_address}"
                logging.debug(
                    "Consul: Path %s, value %s (session %s)",
                    path,
                    json_string,
                    self.node_health_session,
                )

                put_result = self.client.kv.put(
                    path, json_string, acquire=self.node_health_session
                )

                if not put_result:
                    logging.error("Unable to create %s", path)
                    return False

                return True
            except:
                logging.warning(
                    "Unable to register node in Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        logging.error("Unable to register node")
        return False

    def populate_node_info(self, mysql_version=None, server_id=None):
        """
        Populate the node information in Consul
        """

        self.mysql_version = mysql_version
        self.server_id = server_id

        # Allow a minute for node info to be populated
        for _ in range(12):
            try:
                logging.debug("Populate MySQL instance info in Consul")
                ip_address = Consul.getLocalIp()

                get_result = self.client.kv.get(f"{Consul.instances_path}{ip_address}")

                if get_result[1] is None or get_result[1]["Value"] is None:
                    logging.error("Node %s not registered in Consul", ip_address)
                    return False

                node_data = json.loads(get_result[1]["Value"])

                node_data["server_id"] = self.server_id
                node_data["mysql_version"] = self.mysql_version

                json_string = json.dumps(node_data)

                path = f"{Consul.instances_path}{ip_address}"
                logging.debug(
                    "Consul: Path %s, value %s (session %s)",
                    path,
                    json_string,
                    self.node_health_session,
                )

                put_result = self.client.kv.put(
                    path, json_string, acquire=self.node_health_session
                )

                if not put_result:
                    logging.error("Unable to populate node info for %s", path)
                    return False

                return True
            except:
                logging.warning(
                    "Unable to populate node info in Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        logging.error("Unable to populate node info")
        return False

    def node_set_restoring_flag(self, restoring=True):
        """
        Marks the current node as restoring from snapshots. Used to lock snapshot writes until replication is done
        """

        # Allow a minute for node info to be populated
        for _ in range(12):
            try:
                if restoring:
                    logging.debug("Mark MySQL instance as restoring in Consul")
                else:
                    logging.debug("Mark MySQL instance as not restoring in Consul")

                ip_address = Consul.getLocalIp()

                get_result = self.client.kv.get(f"{Consul.instances_path}{ip_address}")
                logging.debug("Got result %s", get_result)

                if get_result[1] is None or get_result[1]["Value"] is None:
                    logging.error("Node %s not registered in Consul", ip_address)
                    return False

                node_data = json.loads(get_result[1]["Value"])
                node_data["restoring"] = restoring

                json_string = json.dumps(node_data)

                path = f"{Consul.instances_path}{ip_address}"
                logging.debug(
                    "Consul: Path %s, value %s (session %s)",
                    path,
                    json_string,
                    self.node_health_session,
                )

                put_result = self.client.kv.put(
                    path, json_string, acquire=self.node_health_session
                )

                if not put_result:
                    logging.error("Unable to mark restoring flag on %s", path)
                    return False

                return True
            except:
                logging.warning(
                    "Unable to mark node as restoring in Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        logging.error("Unable to mark node as restoring")
        return False

    def node_set_snapshotting_flag(self, snapshotting=True):
        """
        Marks the current node as snapshotting a new SQL backup. Only informational - snapshotting is
        done nearly always from a replica node, which is already read-only.
        """

        # Allow a minute for node info to be populated
        for _ in range(12):
            try:
                if snapshotting:
                    logging.debug("Mark MySQL instance as snapshotting in Consul")
                else:
                    logging.debug("Mark MySQL instance as not snapshotting in Consul")

                ip_address = Consul.getLocalIp()

                get_result = self.client.kv.get(f"{Consul.instances_path}{ip_address}")
                logging.debug("Got result %s", get_result)

                if get_result[1] is None or get_result[1]["Value"] is None:
                    logging.error("Node %s not registered in Consul", ip_address)
                    return False

                node_data = json.loads(get_result[1]["Value"])
                node_data["snapshotting"] = snapshotting

                json_string = json.dumps(node_data)

                path = f"{Consul.instances_path}{ip_address}"
                logging.debug(
                    "Consul: Path %s, value %s (session %s)",
                    path,
                    json_string,
                    self.node_health_session,
                )

                put_result = self.client.kv.put(
                    path, json_string, acquire=self.node_health_session
                )

                if not put_result:
                    logging.error("Unable to mark snapshotting flag on %s", path)
                    return False

                return True
            except:
                logging.warning(
                    "Unable to mark node as snapshotting in Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        logging.error("Unable to mark node as snapshotting")
        return False

    def are_nodes_restoring(self):
        """
        Check if any nodes are restoring from snapshots
        """

        # Allow 3 minutes of retries
        for _ in range(36):
            try:
                logging.debug("Check if any nodes are restoring from snapshots")

                result = self.client.kv.get(Consul.instances_path, recurse=True)
                logging.debug("Got result %s", result)

                if result[1] is not None:
                    for node in result[1]:
                        node_value = node["Value"]
                        node_data = json.loads(node_value)

                        if not "restoring" in node_data:
                            logging.error("Restoring flag missing in %s", node)
                            continue

                        if node_data["restoring"] is True:
                            logging.debug("Node %s is restoring", node_data)
                            return True

                return False
            except:
                logging.warning(
                    "Unable to get registered nodes from Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        return False

    def are_nodes_snapshotting(self):
        """
        Check if any nodes are creating a snapshot
        """

        # Allow 3 minutes of retries
        for _ in range(36):
            try:
                logging.debug("Check if any nodes are creating the snapshot")

                result = self.client.kv.get(Consul.instances_path, recurse=True)
                logging.debug("Got result %s", result)

                if result[1] is not None:
                    for node in result[1]:
                        node_value = node["Value"]
                        node_data = json.loads(node_value)

                        if not "snapshotting" in node_data:
                            logging.error("Snapshotting flag missing in %s", node)
                            continue

                        if node_data["snapshotting"] is True:
                            logging.debug("Node %s is snapshotting", node_data)
                            return True

                return False
            except:
                logging.warning(
                    "Unable to get registered nodes from Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        return False

    def refresh_sessions(self):
        """
        Refresh the active sessions
        """
        logging.debug("Keeping Consul sessions alive")
        logging.debug("Refreshing session %s", self.node_health_session)

        # Allow 35 seconds to refresh session before recreating the session
        for _ in range(7):
            try:
                self.client.session.renew(self.node_health_session)
                return True
            except:
                logging.warning(
                    "Unable to refresh session %s, retrying in 5 seconds",
                    self.node_health_session,
                )
                time.sleep(5)
                continue

        # If the session is unable to be refreshed, try to recreate it and re-register the node, as it will be
        # automatically delisted by the old session's lock being removed.
        self.node_health_session = None

        try:
            self.create_node_health_session()
            self.register_node()
            self.populate_node_info(self.mysql_version, self.server_id)
        except:
            logging.error(
                "Unable to recreate the node health session, something is wrong with Consul"
            )

        return False

    def destroy_session(self):
        """
        Destory a previosly registered session
        """

        if self.node_health_session is None:
            logging.debug("No session to destroy")
            return True

        for _ in range(6):
            try:
                self.client.session.destroy(self.node_health_session)
                break
            except:
                logging.warning(
                    "Unable to destroy session %s, retrying in 5 seconds",
                    self.node_health_session,
                )
                time.sleep(5)
                continue

        return True

    @staticmethod
    def agent_start():
        """
        Start the local Consul agent.
        """
        if Consul.getLocalIp() is None:
            logging.error(
                "Unable to determine local IP address, cannot start Consul agent"
            )
            sys.exit(1)

        logging.info("Starting Consul Agent")
        consul_args = ["consul"]
        consul_args.append("agent")
        consul_args.append("-data-dir")
        consul_args.append("/tmp/consul")

        consul_args.append("-bind")
        consul_args.append(Consul.getLocalIp())

        consul_args.append("-client")
        consul_args.append("0.0.0.0")

        consul_args.append("-server")

        consul_args.append("-retry-join")
        consul_args.append(
            f"tasks.{Utils.get_envvar_or_secret('CONSUL_BOOTSTRAP_SERVICE', 'mysql')}"
        )

        consul_args.append("-bootstrap-expect")
        consul_args.append(Utils.get_envvar_or_secret("CONSUL_BOOTSTRAP_EXPECT", "3"))

        if (
            Utils.get_envvar_or_secret("CONSUL_ENABLE_UI", "false").lower() == "true"
            or Utils.get_envvar_or_secret("CONSUL_ENABLE_UI", "false") == "1"
        ):
            consul_args.append("-ui")

        logging.info("Consul args: %s", consul_args)

        # Run process in background
        consul_process = subprocess.Popen(consul_args)
        logging.info("Consul agent started with PID %s", consul_process.pid)

        time.sleep(1)

        return consul_process

    @staticmethod
    def getLocalIp():
        """
        Get the local IP, based on the service being bootstrapped
        """
        for _ in range(300):
            try:
                logging.debug(
                    f"Determining local IP address by querying Docker DNS - tasks.{Utils.get_envvar_or_secret('CONSUL_BOOTSTRAP_SERVICE', 'mysql')}"
                )
                ip_addresses = socket.gethostbyname_ex(
                    f"tasks.{Utils.get_envvar_or_secret('CONSUL_BOOTSTRAP_SERVICE', 'mysql')}"
                )[2]

                for interface in netifaces.interfaces():
                    if interface == "lo":
                        continue

                    for addressInfo in netifaces.ifaddresses(interface)[
                        netifaces.AF_INET
                    ]:
                        if addressInfo["addr"] in ip_addresses:
                            logging.debug(
                                "Found local IP %s on interface %s",
                                addressInfo["addr"],
                                interface,
                            )
                            return addressInfo["addr"]
            except:
                logging.warning("Unable to determine local IP address, retrying")
                time.sleep(1)

        return None
