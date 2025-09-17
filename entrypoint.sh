#!/bin/bash
#
# Start the MySQL cluster manager
#
########################

# Exit on error
set -e

exec ./mysql_cluster_manager.py join_or_bootstrap
