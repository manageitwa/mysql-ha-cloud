"""This file is part of the MySQL cluster manager"""

import os
import sys
import time
import json
import logging
import threading
import subprocess
import socket

import netifaces
import consul as pyconsul

from mcm.utils import Utils

class Consul:

    """
    This class encapsulates all Consul related things
    """

    # The signeton instance
    __instance = None

    # Retry counter for operations
    retry_counter = 30

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
        for _ in range(Consul.retry_counter):
            try:
                self.client = pyconsul.Consul()
            except:
                logging.warning("Unable to connect to Consul, retrying in 10 seconds")
                time.sleep(10)
                continue

        if not self.client:
            raise Exception("Unable to establish a connection with Consul")

        self.active_sessions = []
        self.node_health_session = self.create_node_health_session()

        # The session auto refresh thread
        self.auto_refresh_thread = None
        self.run_auto_refresh_thread = False

    @staticmethod
    def get_instance():
        """ Static access method. """
        if Consul.__instance is None:
            return Consul()

        return Consul.__instance

    def start_session_auto_refresh_thread(self):
        """
        Start the session auto refresh thread
        """
        logging.info("Starting the Consul session auto refresh thread")
        self.run_auto_refresh_thread = True
        self.auto_refresh_thread = threading.Thread(target=self.auto_refresh_sessions, args=())
        self.auto_refresh_thread.start()

    def auto_refresh_sessions(self):
        """
        Auto refresh the active sessions
        """
        while self.run_auto_refresh_thread:
            logging.debug("Refreshing active consul sessions from auto refresh thread")
            self.refresh_sessions()
            time.sleep(2)

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

        for _ in range(Consul.retry_counter):
            try:
                session = self.create_session(
                    name=Consul.instances_session_key,
                    behavior='delete', ttl=15, lock_delay=0)

                return session
            except:
                logging.warning("Unable to create a session in Consul, retrying in 10 seconds")
                time.sleep(10)

        if session is None:
            raise Exception("Unable to create node health session")


    def get_all_registered_nodes(self):
        """
        Get all registered MySQL nodes
        """
        mysql_nodes = []

        for _ in range(Consul.retry_counter):
            try:
                result = self.client.kv.get(Consul.instances_path, recurse=True)

                if result[1] is not None:
                    for node in result[1]:
                        node_value = node['Value']
                        node_data = json.loads(node_value)

                        if not "ip_address" in node_data:
                            logging.error("ip_address missing in %s", node)
                            continue

                        if "replicating" in node_data and node_data["replicating"] is True:
                            logging.debug("Skipping node %s as it is currently replicating", node_data)
                            continue

                        ip_address = node_data["ip_address"]
                        mysql_nodes.append(ip_address)

                return mysql_nodes
            except:
                logging.warning("Unable to get registered nodes from Consul, retrying in 10 seconds")
                time.sleep(10)

        return mysql_nodes

    def get_mysql_server_id(self):
        """
        Get the MySQL server id from consul

        Try to get existing value and update to +1
          * If Update fails, retry
          * If Key not exists, try to create
        """
        for _ in range(Consul.retry_counter):
            try:
                result = self.client.kv.get(Consul.kv_server_id)

                # Create new key
                if result[1] is None:
                    logging.debug("Old serverkey %s not found, preparing new one",
                                Consul.kv_server_id)

                    json_string = json.dumps({'last_used_id': 1})

                    # Try to create
                    put_result = self.client.kv.put(Consul.kv_server_id, json_string, cas=0)
                    if put_result is True:
                        logging.debug("Created new key, started new server counter")
                        return 1

                    logging.debug("New key could not be created, retrying")
                    continue

                # Updating existing key
                logging.debug("Updating existing key %s", result)
                json_string = result[1]['Value']
                version = result[1]['ModifyIndex']
                server_data = json.loads(json_string)

                if not "last_used_id" in server_data:
                    logging.error("Invalid JSON returned (missing last_used_id) %s",
                                json_string)

                server_data['last_used_id'] = server_data['last_used_id'] + 1
                json_string = json.dumps(server_data)
                put_result = self.client.kv.put(Consul.kv_server_id, json_string, cas=version)

                if put_result is True:
                    logging.debug("Successfully updated consul value %s, new server_id is %i",
                                put_result, server_data['last_used_id'])
                    return server_data['last_used_id']
            except:
                logging.debug("Unable to get MYSQL server ID, retrying in 10 seconds")
                time.sleep(10)

        raise Exception("Unable to determine server id")

    def is_replication_leader(self):
        """
        Test if this is the MySQL replication leader or not
        """

        for _ in range(Consul.retry_counter):
            try:
                result = self.client.kv.get(Consul.replication_leader_path)

                if result[1] is None:
                    logging.debug("No replication leader node available")
                    return False

                leader_session = result[1]['Session']

                logging.debug("Replication leader is %s, we are %s",
                            leader_session, self.node_health_session)

                return leader_session == self.node_health_session
            except:
                logging.warning("Unable to determine replication leader from Consul, retrying in 10 seconds")
                time.sleep(10)

        return False

    def get_replication_leader_ip(self):
        """
        Get the IP of the current replication ledear
        """

        for _ in range(Consul.retry_counter):
            try:
                result = self.client.kv.get(Consul.replication_leader_path)

                if result[1] is None:
                    return None

                json_string = result[1]['Value']
                server_data = json.loads(json_string)

                if not "ip_address" in server_data:
                    logging.error("Invalid JSON returned from replication ledader (missing server_id) %s",
                                json_string)

                return server_data['ip_address']
            except:
                logging.warning("Unable to get replication leader IP from Consul, retrying in 10 seconds")
                time.sleep(10)

        return None

    def try_to_become_replication_leader(self):
        """
        Try to become the new replication leader
        """

        for _ in range(Consul.retry_counter):
            try:
                result = self.client.kv.get(Consul.replication_leader_path)

                if result[1] is None:
                    logging.debug("Register MySQL instance in Consul")
                    ip_address = Consul.getLocalIp()

                    json_string = json.dumps({
                        'ip_address': ip_address
                    })

                    put_result = self.client.kv.put(Consul.replication_leader_path,
                                                    json_string,
                                                    acquire=self.node_health_session)

                    if put_result:
                        logging.info("We are the new replication leader")
                    else:
                        logging.debug("Unable to become replication leader, retry")

                    return put_result

                return False
            except:
                logging.warning("Unable to become replication leader due to error communicating with Consul, retrying in 10 seconds")
                time.sleep(10)

        return False


    def register_service(self, leader=False, port=3306):
        """
        Register the MySQL primary service
        """

        for _ in range(Consul.retry_counter):
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
                    logging.debug("Unregister old service %s (%s)", service_id, all_services)
                    self.client.agent.service.deregister(service_id)

                # Register new service
                logging.info("Register new service_id=%s, tags=%s", service_id, tags)
                self.client.agent.service.register("mysql", service_id=service_id, port=port, tags=tags)

                return True
            except:
                logging.warning("Unable to register service in Consul, retrying in 10 seconds")
                time.sleep(10)

        return False

    def register_node(self):
        """
        Register the node in Consul
        """

        for _ in range(Consul.retry_counter):
            try:
                logging.debug("Register MySQL instance in Consul")
                ip_address = Consul.getLocalIp()

                json_string = json.dumps({
                    'ip_address': ip_address,
                    'server_id': '',
                    'mysql_version': '',
                    'restoring': False,
                })

                path = f"{Consul.instances_path}{ip_address}"
                logging.debug("Consul: Path %s, value %s (session %s)",
                            path, json_string, self.node_health_session)

                put_result = self.client.kv.put(path, json_string, acquire=self.node_health_session)

                if not put_result:
                    logging.error("Unable to create %s", path)
                    return False

                return True
            except:
                logging.warning("Unable to register node in Consul, retrying in 10 seconds")
                time.sleep(10)

        logging.error("Unable to register node")
        return False

    def populate_node_info(self, mysql_version=None, server_id=None):
        """
        Populate the node information in Consul
        """
        for _ in range(Consul.retry_counter):
            try:
                logging.debug("Register MySQL instance in Consul")
                ip_address = Consul.getLocalIp()

                get_result = self.client.kv.get(f"{Consul.instances_path}{ip_address}")

                if get_result[1] is None or get_result[1]['Value'] is None:
                    logging.error("Node %s not registered in Consul", ip_address)
                    return False

                node_data = json.loads(get_result[1]['Value'])

                node_data['server_id'] = server_id
                node_data['mysql_version'] = mysql_version

                json_string = json.dumps(node_data)

                path = f"{Consul.instances_path}{ip_address}"
                logging.debug("Consul: Path %s, value %s (session %s)",
                            path, json_string, self.node_health_session)

                put_result = self.client.kv.put(path, json_string, acquire=self.node_health_session)

                if not put_result:
                    logging.error("Unable to create %s", path)
                    return False

                return True
            except:
                logging.warning("Unable to populate node info in Consul, retrying in 10 seconds")
                time.sleep(10)

        logging.error("Unable to populate node info")
        return False

    def node_set_restoring_flag(self, restoring=True):
        """
        Marks the current node as restoring from snapshots. Used to lock snapshot writes until replication is done
        """
        for _ in range(Consul.retry_counter):
            try:
                if restoring:
                    logging.debug("Mark MySQL instance as restoring in Consul")
                else:
                    logging.debug("Mark MySQL instance as not restoring in Consul")

                ip_address = Consul.getLocalIp()

                get_result = self.client.kv.get(f"{Consul.instances_path}{ip_address}")
                logging.debug("Got result %s", get_result)

                if get_result[1] is None or get_result[1]['Value'] is None:
                    logging.error("Node %s not registered in Consul", ip_address)
                    return False

                node_data = json.loads(get_result[1]['Value'])
                node_data['restoring'] = restoring

                json_string = json.dumps(node_data)

                path = f"{Consul.instances_path}{ip_address}"
                logging.debug("Consul: Path %s, value %s (session %s)",
                            path, json_string, self.node_health_session)

                put_result = self.client.kv.put(path, json_string, acquire=self.node_health_session)

                if not put_result:
                    logging.error("Unable to create %s", path)
                    return False

                return True
            except:
                logging.warning("Unable to mark node as restoring in Consul, retrying in 10 seconds")
                time.sleep(10)

        logging.error("Unable to mark node as restoring")
        return False

    def are_nodes_restoring(self):
        """
        Check if any nodes are restoring from snapshots
        """
        for _ in range(Consul.retry_counter):
            try:
                logging.debug("Check if any nodes are restoring from snapshots")

                result = self.client.kv.get(Consul.instances_path, recurse=True)
                logging.debug("Got result %s", result)

                if result[1] is not None:
                    for node in result[1]:
                        node_value = node['Value']
                        node_data = json.loads(node_value)

                        if not "restoring" in node_data:
                            logging.error("Restoring flag missing in %s", node)
                            continue

                        if node_data["restoring"] is True:
                            logging.debug("Node %s is restoring", node_data)
                            return True

                return False
            except:
                logging.warning("Unable to get registered nodes from Consul, retrying in 10 seconds")
                time.sleep(10)

        return False

    def refresh_sessions(self):
        """
        Refresh the active sessions
        """
        logging.debug("Keeping Consul sessions alive")

        for session in self.active_sessions:
            logging.debug("Refreshing session %s", session)

            for _ in range(Consul.retry_counter):
                try:
                    self.client.session.renew(session)
                    break
                except:
                    logging.warning("Unable to refresh session %s, retrying in 10 seconds", session)
                    time.sleep(10)
                    continue


    def create_session(self, name, behavior='release', ttl=None, lock_delay=15):
        """
        Create a new session.

        Keep in mind that the real invalidation is around 2*ttl
        see https://github.com/hashicorp/consul/issues/1172
        """

        for _ in range(Consul.retry_counter):
            try:
                session_id = self.client.session.create(name=name,
                                                        behavior=behavior,
                                                        ttl=ttl,
                                                        lock_delay=lock_delay)

                # Keep session for auto refresh
                self.active_sessions.append(session_id)

                logging.debug("Created new session on node %s named %s", name, session_id)

                return session_id
            except:
                logging.warning("Unable to create session %s, retrying in 10 seconds", name)
                time.sleep(10)

        return None


    def destroy_session(self, session_id):
        """
        Destory a previosly registered session
        """

        if not session_id in self.active_sessions:
            return False

        for _ in range(Consul.retry_counter):
            try:
                self.active_sessions.remove(session_id)
                self.client.session.destroy(session_id)
                break
            except:
                logging.warning("Unable to destroy session %s, retrying in 10 seconds", session_id)
                time.sleep(10)
                continue

        return True

    @staticmethod
    def agent_start():
        """
        Start the local Consul agent.
        """
        logging.info("Starting Consul Agent")
        consul_args = ["consul"]
        consul_args.append("agent")
        consul_args.append("-data-dir")
        consul_args.append("/tmp/consul")

        if Consul.getLocalIp() is None:
            logging.error("Unable to determine local IP address, cannot start Consul agent")
            sys.exit(1)

        consul_args.append("-bind")
        consul_args.append(Consul.getLocalIp())

        consul_args.append("-client")
        consul_args.append("0.0.0.0");

        consul_args.append("-server")

        consul_args.append("-retry-join")
        consul_args.append(f'tasks.{Utils.get_envvar_or_secret("CONSUL_BOOTSTRAP_SERVICE", "mysql")}')

        consul_args.append("-bootstrap-expect")
        consul_args.append(Utils.get_envvar_or_secret("CONSUL_BOOTSTRAP_EXPECT", "3"))

        if (Utils.get_envvar_or_secret("CONSUL_ENABLE_UI", "false").lower() == "true" or
            Utils.get_envvar_or_secret("CONSUL_ENABLE_UI", "false") == "1"):
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
        ip_addresses = socket.gethostbyname_ex(
            f'tasks.{Utils.get_envvar_or_secret("CONSUL_BOOTSTRAP_SERVICE", "mysql")}')[2]

        for interface in netifaces.interfaces():
            if interface == 'lo':
                continue

            for addressInfo in netifaces.ifaddresses(interface)[netifaces.AF_INET]:
                if addressInfo['addr'] in ip_addresses:
                    logging.debug("Found local IP %s on interface %s", addressInfo['addr'], interface)
                    return addressInfo['addr']

        return None
