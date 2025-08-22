# MySQL 8.4 High Availability Cloud Container

This project is a fork of the excellent [MySQL-HA-Cloud](https://github.com/jnidzwetzki/mysql-ha-cloud) project by [Jan Nidzwetzki](https://github.com/jnidzwetzki), which has unfortunately been archived and seemingly never came out of beta. It maintains the same goal of providing a high-availability MySQL server cluster using containers, but has been upgraded to use MySQL 8.4, the current LTS version, and includes upgrades to all components involved.

## Features

- Automatic MySQL replication leader election using [Consul](https://developer.hashicorp.com/consul) as a backend.
- Automatic configuration of MySQL nodes as replication leader and workers.
- Failover is handled automatically for both MySQL and the underlying Consul raft. Availability should survive both services' leaders going down, as long as at least 2 servers remain active.
- Automatic, atomic snapshotting of database to recover from catastrophic failures or bootstrapping new nodes.
- Transparent connection routing for read/write splitting using [ProxySQL](https://proxysql.com/).
- Horizontally scalable MySQL deployment - increase and decrease nodes as necessary.
- Compatible with Docker Swarm ~~and Kubernetes~~ ([See notes](#notes-and-faq)).

## Changes from original project

A couple of changes have been made to this fork compared to the original project:

- **Major change:** Due to changes to DNS and virtual IP resolution of the containers in order for Consul and MySQL to talk to one another, Kubernetes is no longer supported in this fork for now. We are happy to accept a PR that reintroduces it. ([See notes](#notes-and-faq))
- MinIO storage of backups has been removed entirely from this project and atomic snapshotting is now used. ([See notes](#notes-and-faq))
- Consul is no longer required as a separate service, as the embedded Consul CLI is now used as a server agent in each node. The resolution of the DNS is handled internally.
- The image is now based on the official MySQL 8.4 image, which is based on Oracle Linux 9 as opposed to Debian Bookworm.
- Nodes should now gracefully remove themselves from the cluster if they are stopped by Docker, or if the daemon crashes at any point, unless the container is killed by a SIGKILL signal. However, the other nodes *should* be able to recover from this scenario as well, as long as the tolerance is maintained.
- Support for Docker secrets has been introduced - all environment variables can be suffixed with `_FILE` to read from a file instead of passing the value directly.
- Some environment variables have been dropped, and others have been renamed. Please see environment variables defined below.

## Deployment



## Environment variables

The following environment variables are used to configure this service.

| Variable | Required | Default | Description |
| -------- | -------- | ------- | ----------- |
| `CONSUL_BOOTSTRAP_SERVICE` | No | `"mysql"` | The name of the service to bootstrap the Consul agent for. This should match your service name. |
| `CONSUL_BOOTSTRAP_EXPECT` | No | `"3"` | The number of instances to expect in the cluster in order for Consul to bootstrap. We have set this to 3 by default for failover, and should be used as a minimum. This _does not_ have to match your number of replicas, as long as your number of replicas is greater than or equal to this number. |
| `CONSUL_ENABLE_UI` | No | `"false"` | If `"true"` or `1`, the Consul UI will be enabled. This may reveal information about your cluster, so only enable it if you can secure it. |
| `MYSQL_ROOT_PASSWORD` | **Yes** | *None* | Defines the root password assigned to all nodes. This must be specified in order for nodes to be bootstrapped. It is recommended that you use a secret to provide this value. |
| `MYSQL_USER` | **Yes** | *None* | Defines a username that will be created on initialisation. |
| `MYSQL_PASSWORD` | **Yes** | *None* | Defines the password for the `MYSQL_USER` account. It is recommended that you use a secret to provide this value. |
| `MYSQL_BACKUP_USER` | **Yes** | *None* | Defines a username for an account, created on initialisation, that will be used by XtraBackup to take snapshots of the database. |
| `MYSQL_BACKUP_PASSWORD` | **Yes** | *None* | Defines the password for the `MYSQL_BACKUP_USER` account. It is recommend that you use a secret to provide this value. |
| `MYSQL_REPLICATION_USER` | **Yes** | *None* | Defines a username for an account, created on initialisation, that will be used by the nodes for replication. |
| `MYSQL_REPLICATION_PASSWORD` | **Yes** | *None* | Defines the password for the `MYSQL_REPLICATION_USER` account. It is recommended that you use a secret to provide this value. |

All environment variables above can be suffixed with `_FILE`, which can be used to point to a path where a secret is made available - for example, you could set `MYSQL_USER_FILE` to point to `/run/secrets/MYSQL_USER`, which would then use the value of secret `MYSQL_USER` to define the application user.

## Volumes

Only one volume is strictly required - the `/snapshots` volume. This volume stores the current database snapshot used for bootstrapping new nodes or recovering the cluster. The volume **must** be accessible to all nodes - read and write - so this must be bound to shared storage, such as Ceph or NFS.

## Architecture

Each node running this image contains the following:

- A MySQL 8.4 server instance
- A Consul 1.21 agent running in server mode
- ProxySQL 3.0.2 installed
- The contents of this repo's `mysql_cluster_manager` directory, acting as a Python 3 entrypoint/daemon.
- A `snapshots` folder that contains the current snapshot of the database. This is a full [XtraBackup](https://www.percona.com/mysql/software/percona-xtrabackup) snapshot of the database, at most, 5 minutes old.

It is expected that at least 3 nodes are made available as part of this service to provide a tolerance of 1 lost node. Each node can become a MySQL leader and/or a [Consul](https://developer.hashicorp.com/consul) leader, making all other nodes a follower for each service. These nodes must be connected to the same overlay network in order to communicate with one another. Consul acts as the "source of truth" for the purposes of determining available MySQL nodes and defining which MySQL node is the leader.

The MySQL leader node becomes a MySQL "read-write" node, with all writes being made to this node and then replicated to all other nodes, which are "read-only". Replication SQL queries are run on each node to set the MySQL replica state on each node to match their purpose.

The standard MySQL port `3306` is routed to [ProxySQL](https://proxysql.com/), which is installed on each node and is kept appraised of the layout of the network and routes read and write queries accordingly. This means that each node can receive SQL queries and they will be routed to the correct node, allowing you to load balance the servers (either with Docker's replica capabilities, or externally) and enacts the failover capability. If a node goes down, the Consul network will inform the daemon on each node to remove the lost node from ProxySQL on that node so that the remaining nodes will no longer route queries to the lost node.

The snapshots are a point-in-time backup of the database, run every 5 minutes. These are used to bootstrap any new nodes that are added as replicas of the leader, and can also be used to recover the cluster entirely if it brought down. These snapshots are atomic - only one server will be able to replace the snapshot, and the current snapshot is not overwritten unless the snapshot completes successfully. To re-initialise the cluster from scratch, you only need to bring down the cluster and delete the snapshot.

## Notes and FAQ

- **Why is Kubernetes no longer supported?** \
  The chief reason at the moment is simply that we do not use Kubernetes internally, so we have no way of testing it. We also made some significant changes to the DNS resolution of the instances in order to allow Consul to communicate effectively, but these changes _potentially_ rely on features inherent in Docker Swarm. \
  \
  The original project used a single interface environment variable to determine which IP Consul binds to when creating the agents. In Docker Swarm, this resulted in issues with multiple containers talking to one another, because this interface could either not exist or be taken by another overlay network as Docker Swarm seemingly assigns the interface names at random. In order for us to avoid this issue, we now use a environment variable to define the service name, and resolve the "`tasks.<service_name>`" DNS entry automatically created by Docker Swarm to get the IP of all containers participating in the Swarm, and determine the correct IP for Consul from this list. We also use the same DNS entry to define the nodes for Consul to attempt to join to. Our understanding is that Kubernetes does not provide the exact same functionality (although we concede that it probably does support something similar).
- **Why was the MinIO storage removed?** \
  From our usage of the original project for a number of years (even in beta!), we found that the MinIO storage, whilst doing what it was intended to do, added additional storage considerations and complexity that we felt were not necessary for this project. In particular, the original project took backups every 6 hours and stored 7 days worth of backups. If a catastrophic failure resulted in all nodes being lost and having to be rebuilt from backup, it could result in up to 6 hours of data loss which in our view was unacceptable. The resulting 168 copies of backups also took up a lot of space when only the latest backup was really needed for restoration purposes. \
  \
  We feel that backing up the database is a responsibility best left to the user, allowing them to make a decision on how often to take backups and where to store them. Instead, to mitigate the data loss risk, we introduced a snapshot feature to take more regular snapshots (in our case, every 5 minutes). This considerably shrunk the space needed for recovery and provided a much smaller window of data loss in the event of a catastrophic failure. \
  \
  Also, Minio's [licensing](https://github.com/minio/minio/discussions/12157) [shenanigans](https://github.com/minio/object-browser/pull/3509) made us a little uneasy.
