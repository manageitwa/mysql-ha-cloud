"""This file is part of the MySQL cluster manager"""

import base64
import hashlib
import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid

import consul as pyconsul
import netifaces
from consul.callback import CB

from mcm.utils import Utils


class Consul:
    """
    This class encapsulates all Consul related things
    """

    # The signeton instance
    __instance = None

    # KV prefix
    kv_prefix = "mcm/"

    # Instances ID key
    instances_path = kv_prefix + "instances/"

    # Instances session key
    instances_session_key = kv_prefix + "instances"

    # Replication leader path
    replication_leader_path = kv_prefix + "replication_leader"

    # The IP of this node, once found
    local_ip = None

    # Agent data directory
    data_dir = "/tmp/consul"

    # Agent configuration file. Kept in the data directory, as it contains secrets and the data
    # directory is expected to be a tmpfs mount.
    config_file = data_dir + "/mcm.json"

    # The gossip encryption key and ACL token, once determined
    gossip_key = None
    acl_token = None

    # The TTL of the node health session, in seconds. As the session has no health checks, Consul
    # does not invalidate it, and so release the replication leader lock, until at least this long
    # after it was last renewed.
    session_ttl = 15

    # Timeout for requests to the Consul agent, in seconds, so that a hung agent does not block
    request_timeout = 5

    def __init__(self):
        """
        Init the Consul client
        """
        if Consul.__instance is not None:
            raise Exception("This class is a singleton!")

        Consul.__instance = self
        logging.info("Register Consul connection")

        acl_token = Consul.get_acl_token()

        # Allow 30 seconds for Consul agent to start
        for _ in range(6):
            try:
                self.client = pyconsul.Consul(
                    token=acl_token, connections_timeout=Consul.request_timeout
                )
            except:
                logging.warning("Unable to connect to Consul, retrying in 5 seconds")
                time.sleep(5)
                continue

        if not self.client:
            raise Exception("Unable to establish a connection with Consul")

        self.node_health_session = None

        # When the node health session was last created or renewed (monotonic clock)
        self.last_session_renewal = None

        self.create_node_health_session(wait=True)
        self.mysql_version = None
        self.server_id = None

        # Last replication health state published for this node
        self.replication_unhealthy = False

        # Executed transactions published for this node while the replication leader is missing
        self.failover_gtids = None

        # The session auto refresh thread
        self.auto_refresh_thread = None
        self.run_auto_refresh_thread = False

        # Wakes the session auto refresh thread, so that it can be stopped without waiting
        self.auto_refresh_wakeup = threading.Event()

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
        self.auto_refresh_wakeup.clear()
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
            self.auto_refresh_wakeup.wait(5)

    def start_session_keepalive_thread(self):
        """
        Start the thread that renews the node health session every couple of seconds for the life
        of the node. This runs separately from the main event loop, which can be held up by other
        requests to Consul, so that the session is renewed as soon as Consul is available again.
        """
        logging.info("Starting the Consul session keepalive thread")
        threading.Thread(target=self.keep_session_alive, args=(), daemon=True).start()

    def keep_session_alive(self):
        """
        Renew the node health session every couple of seconds. Each attempt is made once, as
        refresh_sessions() recreates the session if it cannot be renewed.
        """
        while True:
            try:
                if self.node_health_session is not None:
                    self.client.session.renew(self.node_health_session)
                    self.last_session_renewal = time.monotonic()
            except Exception:
                logging.debug("Unable to renew session %s", self.node_health_session)

            time.sleep(2)

    def stop_session_auto_refresh_thread(self):
        """
        Stop the session auto refresh thread
        """
        logging.info("Stopping the Consul session auto refresh thread")
        self.run_auto_refresh_thread = False
        self.auto_refresh_wakeup.set()
        if self.auto_refresh_thread is not None:
            self.auto_refresh_thread.join()
            self.auto_refresh_thread = None
        logging.info("Consul session auto refresh thread is stopped")

    def create_node_health_session(self, wait=False):
        """
        Create the node health session
        all created KV entries automatically removed
        on session destory.

        If wait is set, retry until the session is created, such as while a node is starting and the
        Consul cluster has no leader until it is recovered. Otherwise allow 30 seconds.
        """

        session = None
        attempts = 0

        while wait or attempts < 6:
            attempts += 1

            try:
                # No health checks are attached, so the session is only invalidated by its TTL.
                # By default, the session would also be invalidated as soon as the Consul agents
                # mark this node as failed, which can happen before this node has noticed that it
                # cannot renew the session and fenced itself.
                self.node_health_session = self.client.session.create(
                    name=Consul.instances_session_key,
                    behavior="delete",
                    ttl=Consul.session_ttl,
                    lock_delay=0,
                    checks=[],
                )
                self.last_session_renewal = time.monotonic()

                return self.node_health_session
            except:
                logging.warning(
                    "Unable to create a session in Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        if session is None:
            raise Exception("Unable to create node health session")

    def get_registered_node_data(self):
        """
        Get the data of all registered MySQL nodes
        """
        nodes = []

        # Allow 3 minutes of retries to get the nodes as this will usually only fail on a potential
        # network downtime
        for _ in range(36):
            try:
                result = self.client.kv.get(Consul.instances_path, recurse=True)

                if result[1] is not None:
                    for node in result[1]:
                        node_data = Consul.parse_node_data(node["Value"])

                        # Each node is registered under its own IP address
                        if (
                            node_data is None
                            or node["Key"]
                            != f"{Consul.instances_path}{node_data['ip_address']}"
                        ):
                            logging.error("Invalid node data in %r, ignoring", node["Key"])
                            continue

                        nodes.append(node_data)

                return nodes
            except:
                logging.warning(
                    "Unable to get registered nodes from Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        return nodes

    @staticmethod
    def parse_node_data(value):
        """
        Parse the data of a node from Consul, or return None if it is invalid. The IP address is
        validated, as it is used to configure replication and ProxySQL.
        """
        try:
            node_data = json.loads(value)
        except (TypeError, ValueError):
            return None

        if not isinstance(node_data, dict) or not isinstance(
            node_data.get("ip_address"), str
        ):
            return None

        try:
            node_data["ip_address"] = str(ipaddress.ip_address(node_data["ip_address"]))
        except ValueError:
            return None

        if not isinstance(node_data.get("failover_gtids"), (str, type(None))):
            return None

        return node_data

    @staticmethod
    def is_node_available(node_data):
        """
        Check if a node is able to serve queries (not restoring, snapshotting or
        with unhealthy replication)
        """
        if "restoring" in node_data and node_data["restoring"] is True:
            logging.debug("Node %s is currently restoring", node_data)
            return False

        if "snapshotting" in node_data and node_data["snapshotting"] is True:
            logging.debug("Node %s is currently snapshotting", node_data)
            return False

        if (
            "replication_unhealthy" in node_data
            and node_data["replication_unhealthy"] is True
        ):
            logging.debug("Node %s has unhealthy replication", node_data)
            return False

        return True

    def get_registered_node_states(self):
        """
        Get all registered MySQL nodes, mapped to whether they are available to serve queries
        """
        return {
            node_data["ip_address"]: Consul.is_node_available(node_data)
            for node_data in self.get_registered_node_data()
        }

    def holds_replication_leader_lock(self):
        """
        Check once if this node holds the replication leader lock, without retrying. Returns False
        if Consul cannot be reached.
        """
        try:
            result = self.client.kv.get(Consul.replication_leader_path)
        except Exception:
            return False

        return (
            result[1] is not None
            and self.node_health_session is not None
            and result[1].get("Session") == self.node_health_session
        )

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
                Utils.clear_repeated_log("replication_leader_ip")

                if result[1] is None:
                    return None

                leader_data = Consul.parse_node_data(result[1]["Value"])

                if leader_data is None:
                    logging.error("Invalid replication leader data in Consul, ignoring")
                    return None

                ip_address = leader_data["ip_address"]

                # The leader holds its lock with the same session that registered its node, so
                # only accept a leader that is a registered node
                leader_session = result[1].get("Session")
                node = self.client.kv.get(f"{Consul.instances_path}{ip_address}")[1]

                if (
                    leader_session is None
                    or node is None
                    or node.get("Session") != leader_session
                ):
                    logging.warning(
                        "Replication leader %s is not a registered node, ignoring",
                        ip_address,
                    )
                    return None

                return ip_address
            except:
                # This is checked every second, so while Consul has no leader it would otherwise be
                # logged every 5 seconds
                Utils.log_repeated(
                    "replication_leader_ip",
                    logging.WARNING,
                    "Unable to get replication leader IP from Consul, retrying in 5 seconds",
                )
                time.sleep(5)

        return None

    def try_to_become_replication_leader(self):
        """
        Try to become the new replication leader
        """

        # Without a session, the leader key would be written without a lock, so it would never be
        # released and no node could become the leader
        if self.node_health_session is None:
            Utils.log_repeated(
                "replication_leader_session",
                logging.WARNING,
                "No Consul session, unable to become replication leader",
            )
            return False

        Utils.clear_repeated_log("replication_leader_session")

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
                        # Known when the node registers again, such as after Consul is recovered
                        "server_id": self.server_id if self.server_id is not None else "",
                        "mysql_version": (
                            self.mysql_version if self.mysql_version is not None else ""
                        ),
                        "snapshotting": False,
                        "restoring": False,
                        "replication_unhealthy": self.replication_unhealthy,
                        "failover_gtids": self.failover_gtids,
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

                node_data = Consul.parse_node_data(get_result[1]["Value"])

                if node_data is None:
                    logging.error("Node %s has invalid data in Consul", ip_address)
                    return False

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

                node_data = Consul.parse_node_data(get_result[1]["Value"])

                if node_data is None:
                    logging.error("Node %s has invalid data in Consul", ip_address)
                    return False
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

                node_data = Consul.parse_node_data(get_result[1]["Value"])

                if node_data is None:
                    logging.error("Node %s has invalid data in Consul", ip_address)
                    return False
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

    def node_set_replication_unhealthy_flag(self, unhealthy=True):
        """
        Marks the current node as having unhealthy replication. Unhealthy nodes
        are set to OFFLINE_SOFT in ProxySQL to prevent serving stale reads.
        """

        # Allow a minute for node info to be populated
        for _ in range(12):
            try:
                if unhealthy:
                    logging.debug(
                        "Mark MySQL instance as replication unhealthy in Consul"
                    )
                else:
                    logging.debug(
                        "Mark MySQL instance as replication healthy in Consul"
                    )

                ip_address = Consul.getLocalIp()

                get_result = self.client.kv.get(f"{Consul.instances_path}{ip_address}")
                logging.debug("Got result %s", get_result)

                if get_result[1] is None or get_result[1]["Value"] is None:
                    logging.error("Node %s not registered in Consul", ip_address)
                    return False

                node_data = Consul.parse_node_data(get_result[1]["Value"])

                if node_data is None:
                    logging.error("Node %s has invalid data in Consul", ip_address)
                    return False
                node_data["replication_unhealthy"] = unhealthy

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
                    logging.error(
                        "Unable to mark replication unhealthy flag on %s", path
                    )
                    return False

                self.replication_unhealthy = unhealthy
                return True
            except:
                logging.warning(
                    "Unable to mark node as replication unhealthy in Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        logging.error("Unable to mark node as replication unhealthy")
        return False

    def node_set_failover_gtids(self, gtids=None):
        """
        Publishes the transactions executed on the current node while the replication leader is
        missing, so that replicas can determine which of them has the most recent data to become
        the new leader. Cleared (None) once a leader is available.
        """

        # Allow a minute for node info to be populated
        for _ in range(12):
            try:
                logging.debug("Set failover GTIDs in Consul to %s", gtids)

                ip_address = Consul.getLocalIp()

                get_result = self.client.kv.get(f"{Consul.instances_path}{ip_address}")
                logging.debug("Got result %s", get_result)

                if get_result[1] is None or get_result[1]["Value"] is None:
                    logging.error("Node %s not registered in Consul", ip_address)
                    return False

                node_data = Consul.parse_node_data(get_result[1]["Value"])

                if node_data is None:
                    logging.error("Node %s has invalid data in Consul", ip_address)
                    return False
                node_data["failover_gtids"] = gtids

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
                    logging.error("Unable to set failover GTIDs on %s", path)
                    return False

                self.failover_gtids = gtids
                return True
            except:
                logging.warning(
                    "Unable to set failover GTIDs in Consul, retrying in 5 seconds"
                )
                time.sleep(5)

        logging.error("Unable to set failover GTIDs")
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
                        node_data = Consul.parse_node_data(node["Value"])

                        if node_data is None:
                            logging.error("Invalid node data in %r, ignoring", node["Key"])
                            continue

                        if not "restoring" in node_data:
                            logging.error("Restoring flag missing in %r", node["Key"])
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
                        node_data = Consul.parse_node_data(node["Value"])

                        if node_data is None:
                            logging.error("Invalid node data in %r, ignoring", node["Key"])
                            continue

                        if not "snapshotting" in node_data:
                            logging.error("Snapshotting flag missing in %r", node["Key"])
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
                self.last_session_renewal = time.monotonic()
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
    def agent_start(bootstrap_expect=None):
        """
        Start the local Consul agent. If bootstrap_expect is given, the agent bootstraps a new
        Consul cluster once that many servers are available. Otherwise it only joins an existing
        Consul cluster.
        """
        if Consul.getLocalIp() is None:
            logging.error(
                "Unable to determine local IP address, cannot start Consul agent"
            )
            sys.exit(1)

        ui_enabled = (
            Utils.get_envvar_or_secret("CONSUL_ENABLE_UI", "false").lower() == "true"
            or Utils.get_envvar_or_secret("CONSUL_ENABLE_UI", "false") == "1"
        )

        Consul.write_agent_config()

        logging.info("Starting Consul Agent")
        consul_args = ["consul"]
        consul_args.append("agent")
        consul_args.append("-data-dir")
        consul_args.append(Consul.data_dir)

        consul_args.append("-config-file")
        consul_args.append(Consul.config_file)

        # Gossip and server RPC use the bind address, so the cluster still communicates over the
        # overlay network
        consul_args.append("-bind")
        consul_args.append(Consul.getLocalIp())

        # The HTTP API is only used locally, so it is only made available to other hosts if the UI
        # is enabled
        consul_args.append("-client")
        consul_args.append("0.0.0.0" if ui_enabled else "127.0.0.1")

        consul_args.append("-server")

        consul_args.append("-retry-interval")
        consul_args.append("5s")

        consul_args.append("-retry-join")
        consul_args.append(
            f"tasks.{Utils.get_envvar_or_secret('CONSUL_BOOTSTRAP_SERVICE', 'mysql')}"
        )

        if bootstrap_expect is not None:
            consul_args.append("-bootstrap-expect")
            consul_args.append(str(bootstrap_expect))

        if ui_enabled:
            consul_args.append("-ui")

        logging.info("Consul args: %s", consul_args)

        # Run process in background
        consul_process = subprocess.Popen(consul_args)
        logging.info("Consul agent started with PID %s", consul_process.pid)

        time.sleep(1)

        return consul_process

    @staticmethod
    def agent_restart(consul_process, bootstrap_expect):
        """
        Stop the local Consul agent, discard its data and start it again, so that it bootstraps or
        joins a new Consul cluster. Used to recover when the Consul cluster has lost the majority of
        its servers, which it cannot recover from on its own.
        """
        logging.warning("Restarting the Consul agent with its data discarded")
        consul_process.terminate()

        try:
            consul_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            consul_process.kill()
            consul_process.wait()

        # The data directory is expected to be a mount point, so only its contents are removed
        for entry in os.listdir(Consul.data_dir):
            path = os.path.join(Consul.data_dir, entry)

            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)

        return Consul.agent_start(bootstrap_expect)

    def has_leader(self):
        """
        Check if the Consul cluster has a leader. Returns None if the local agent cannot be reached.
        """
        try:
            return self.client.status.leader() != ""
        except Exception:
            return None

    def get_raft_peer_ips(self):
        """
        Get the IPs of the Consul servers in this server's Raft configuration. The local copy of
        the configuration is read, so this works when the Consul cluster has no leader.
        """
        result = self.client.http.get(
            CB.json(),
            "/v1/operator/raft/configuration",
            params=[("stale", "")],
            headers={"X-Consul-Token": Consul.get_acl_token()},
        )

        return [server["Address"].rsplit(":", 1)[0] for server in result["Servers"]]

    def reset_session(self):
        """
        Create a new node health session and register this node again, such as after the Consul
        agent has joined a new Consul cluster. The old session is replaced once the new session has
        been created, so that there is always a session for the other threads to use.
        """
        self.create_node_health_session()
        self.register_node()
        self.populate_node_info(self.mysql_version, self.server_id)

    @staticmethod
    def write_agent_config():
        """
        Write the agent configuration file, enabling gossip encryption and ACLs. Secrets are kept
        out of the agent arguments, as these are logged and visible in the process list.
        """
        acl_token = Consul.get_acl_token()

        config = {
            "encrypt": Consul.get_gossip_key(),
            "encrypt_verify_incoming": True,
            "encrypt_verify_outgoing": True,
            "acl": {
                "enabled": True,
                "default_policy": "deny",
                "down_policy": "extend-cache",
                "tokens": {
                    "initial_management": acl_token,
                    "agent": acl_token,
                },
            },
        }

        os.makedirs(Consul.data_dir, exist_ok=True)
        fd = os.open(Consul.config_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as outfile:
            json.dump(config, outfile)

    @staticmethod
    def derive_secret(purpose, length):
        """
        Derive a secret for Consul from the MySQL root password, which is shared by all nodes. Used
        when a secret is not explicitly provided.
        """
        return hashlib.pbkdf2_hmac(
            "sha256",
            Utils.get_envvar_or_secret("MYSQL_ROOT_PASSWORD").encode(),
            f"mysql-ha-cloud/consul/{purpose}".encode(),
            600000,
            dklen=length,
        )

    @staticmethod
    def get_gossip_key():
        """
        Get the gossip encryption key from CONSUL_GOSSIP_KEY, or derive one if not provided
        """
        if Consul.gossip_key is not None:
            return Consul.gossip_key

        gossip_key = Utils.get_envvar_or_secret("CONSUL_GOSSIP_KEY", "")

        if gossip_key == "":
            gossip_key = base64.b64encode(Consul.derive_secret("gossip", 32)).decode()
        else:
            try:
                valid = len(base64.b64decode(gossip_key, validate=True)) in (16, 24, 32)
            except ValueError:
                valid = False

            if not valid:
                logging.error(
                    "CONSUL_GOSSIP_KEY must be a base64 encoded 16, 24 or 32 byte key, such as one generated by \"consul keygen\""
                )
                sys.exit(1)

        Consul.gossip_key = gossip_key
        return Consul.gossip_key

    @staticmethod
    def get_acl_token():
        """
        Get the ACL management token from CONSUL_ACL_TOKEN, or derive one if not provided
        """
        if Consul.acl_token is not None:
            return Consul.acl_token

        acl_token = Utils.get_envvar_or_secret("CONSUL_ACL_TOKEN", "")

        if acl_token == "":
            acl_token = str(uuid.UUID(bytes=Consul.derive_secret("acl", 16), version=4))
        else:
            try:
                acl_token = str(uuid.UUID(acl_token))
            except ValueError:
                logging.error("CONSUL_ACL_TOKEN must be a UUID")
                sys.exit(1)

        Consul.acl_token = acl_token
        return Consul.acl_token

    @staticmethod
    def get_service_task_ips():
        """
        Get the IPs of the service's tasks from the service DNS. Docker only lists running tasks
        that have passed their health check.
        """
        try:
            return socket.gethostbyname_ex(
                f"tasks.{Utils.get_envvar_or_secret('CONSUL_BOOTSTRAP_SERVICE', 'mysql')}"
            )[2]
        except socket.gaierror:
            return []

    @staticmethod
    def getLocalIp():
        """
        Get the local IP, based on the service being bootstrapped
        """
        if Consul.local_ip is not None:
            return Consul.local_ip

        # Docker only adds this node to the service DNS once it passes its health check, so allow a
        # minute for it to appear
        for _ in range(30):
            ip_addresses = Consul.get_service_task_ips()

            for interface in netifaces.interfaces():
                if interface == "lo":
                    continue

                for addressInfo in netifaces.ifaddresses(interface).get(
                    netifaces.AF_INET, []
                ):
                    if addressInfo["addr"] in ip_addresses:
                        logging.debug(
                            "Found local IP %s on interface %s",
                            addressInfo["addr"],
                            interface,
                        )
                        Consul.local_ip = addressInfo["addr"]
                        return Consul.local_ip

            logging.info("Waiting for this node to appear in the service DNS")
            time.sleep(2)

        return None
