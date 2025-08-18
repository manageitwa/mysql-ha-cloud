# MySQL High Availability Cloud Container

This project is a fork of the excellent [MySQL-HA-Cloud](https://github.com/jnidzwetzki/mysql-ha-cloud) project by [Jan Nidzwetzki](https://github.com/jnidzwetzki), which has unfortunately been archived. It maintains the same goal of providing a high-availability MySQL server cluster using containers, but has been upgraded to use MySQL 8.4, the current LTS version, and includes upgrades to all components required.

## Features

- Automatic MySQL replication leader election using Consul as a backend.
- Automatic configuration of MySQL nodes as replication leader and workers, including automatic fail-over.
- Automatic snapshotting of database to recover from catastrophic failures.
- Transparent connection routing for read/write splitting using ProxySQL
- Horizontally scalable
- Compatible with Kubernetes and Docker Swarm

## Changes from original project

A couple of changes have been made to this project compared to the original:

- MinIO has been removed entirely from this project. We felt that backing up the database falls outside of the scope of the project, and is best handled externally. Instead, a volume to hold a snapshot of the database will be used for bootstrapping other nodes or restoring the database.
- The image is now based on the official MySQL 8.4 image, which is based on Oracle Linux 9 as opposed to Debian Bookworm.
- The image now uses the user `1000:1000` instead of `root`.
- Support for Docker secrets has been introduced - all environment variables can be suffixed with `_FILE` to read from a file instead of passing the value directly.
