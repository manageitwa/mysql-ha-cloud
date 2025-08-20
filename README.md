# MySQL 8.4 High Availability Cloud Container

This project is a fork of the excellent [MySQL-HA-Cloud](https://github.com/jnidzwetzki/mysql-ha-cloud) project by [Jan Nidzwetzki](https://github.com/jnidzwetzki), which has unfortunately been archived and seemingly never came out of beta. It maintains the same goal of providing a high-availability MySQL server cluster using containers, but has been upgraded to use MySQL 8.4, the current LTS version, and includes upgrades to all components required.

## Features

- Automatic MySQL replication leader election using [Consul](https://developer.hashicorp.com/consul) as a backend.
- Automatic configuration of MySQL nodes as replication leader and workers, including automatic fail-over.
- Automatic, atomic snapshotting of database to recover from catastrophic failures or bootstrap new nodes.
- Transparent connection routing for read/write splitting using [ProxySQL](https://proxysql.com/)
- Horizontally scalable MySQL deployment
- Compatible with Docker Swarm and Kubernetes

## Changes from original project

A couple of changes have been made to this fork compared to the original project:

- MinIO storage of backups has been removed entirely from this project and atomic snapshotting is now used. ([See notes](#notes-and-faq))
- Consul is no longer required as a separate service, as the embedded Consul CLI is now used as a server agent in each node. This has improved reliability of the communication between nodes, but does mean that it restricts each node to having only one container of this image running on it, as Consul requires host networking to function correctly.
- The image is now based on the official MySQL 8.4 image, which is based on Oracle Linux 9 as opposed to Debian Bookworm.
- Support for Docker secrets has been introduced - nearly all environment variables can be suffixed with `_FILE` to read from a file instead of passing the value directly.

## Notes and FAQ

- This project has only been tested with Docker Swarm, which we use internally. While we believe it should work with Kubernetes using the original instructions, we have not tested out snapshots with Kubernetes, so we do not have any instructions on setting up the snapshots. Please feel free to open a PR if you have tested it with Kubernetes and can provide instructions.
- **Why was the MinIO storage removed?** \
  From our usage of the original project for a number of years (even in beta!), we found that the MinIO storage, whilst doing what it was intended to do, added additional storage considerations and complexity that we felt were not necessary for this project. In particular, the original project took backups every 6 hours and stored 7 days worth of backups. If a catastrophic failure resulted in all nodes being lost and having to be rebuilt from backup, it could result in up to 6 hours of data loss which in our view was unacceptable. The resulting 168 copies of backups also took up a lot of space when only the latest backup was really needed for restoration purposes. \
  \
  We feel that backing up the database is a responsibility best left to the user, allowing them to make a decision on how often to take backups and where to store them. Instead, to mitigate the data loss risk, we introduced a snapshot feature to take more regular snapshots (in our case, every 5 minutes). This considerably shrunk the space needed for recovery and provided a much smaller window of data loss in the event of a catastrophic failure. \
  \
  Also, Minio's [licensing](https://github.com/minio/minio/discussions/12157) [shenanigans](https://github.com/minio/object-browser/pull/3509) made us a little uneasy.
