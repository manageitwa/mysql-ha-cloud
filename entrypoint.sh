#!/usr/bin/env bash

set -e

if [[ "$1" == --* ]]; then
    exec ./mysql_cluster_manager.py join_or_bootstrap "$@"
elif [[ "$1" =~ ^(join_or_bootstrap|mysql_(backup|restore|start|stop|autobackup)|proxysql_init|execute_file)$ ]]; then
    exec ./mysql_cluster_manager.py "$@"
fi

exec "$@"
