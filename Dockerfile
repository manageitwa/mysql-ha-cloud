ARG MYSQL_VERSION=8.4.8
FROM mysql:${MYSQL_VERSION}-oracle

ENV MYSQL_VERSION=${MYSQL_VERSION}
SHELL ["/bin/bash", "-c"]

RUN \
    # Install dependencies
    microdnf install -y \
    libev \
    lz4 \
    perl \
    perl-DBI \
    perl-DBD-MySQL \
    procps \
    python3-devel \
    rsync \
    unzip \
    wget && \
    # Install Percona XtraBackup
    wget https://downloads.percona.com/downloads/Percona-XtraBackup-8.4/Percona-XtraBackup-8.4.0-5/binary/redhat/9/x86_64/percona-xtrabackup-84-8.4.0-5.1.el9.x86_64.rpm -O /tmp/xtrabackup.rpm && \
    rpm -i /tmp/xtrabackup.rpm && \
    rm /tmp/xtrabackup.rpm && \
    # Install Consul CLI
    wget https://releases.hashicorp.com/consul/1.22.5/consul_1.22.5_linux_amd64.zip -O /tmp/consul.zip && \
    echo "58603b87fd085282f882fcd02b5165c93b321692514b2ab822dec8dd4cd028a3 /tmp/consul.zip" | sha256sum -c && \
    unzip /tmp/consul.zip -d /usr/local/bin && \
    rm /usr/local/bin/LICENSE.txt && \
    rm /tmp/consul.zip && \
    # Install ProxySQL
    wget https://github.com/sysown/proxysql/releases/download/v3.0.5/proxysql-3.0.5-1-centos9.x86_64.rpm -O /tmp/proxysql.rpm && \
    echo "40ec35c6863e2a73aa745ce32224c6e189f7e14ecc9e346ceddfe00ea03bc4b1 /tmp/proxysql.rpm" | sha256sum -c && \
    rpm -i /tmp/proxysql.rpm && \
    rm /tmp/proxysql.rpm && \
    # Create directories for Cluster Manager and snapshot
    mkdir /snapshots && \
    mkdir /cluster && \
    # Clean up
    microdnf clean all && \
    rm -rf /var/cache/yum/* && \
    rm -rf /var/lib/rpm/__db* && \
    rm -rf /tmp/*

# Install Cluster Manager and dependencies, and set up volume
WORKDIR /cluster

COPY mysql_cluster_manager/requirements .
COPY mysql_cluster_manager/src .

# Install Python dependencies
ENV PYTHONUSERBASE=/cluster/.prefix
RUN pip3 install --no-cache-dir --user -r requirements

COPY entrypoint.sh .

VOLUME /snapshots
EXPOSE 6032/tcp 6033/tcp
STOPSIGNAL SIGTERM
ENTRYPOINT ["/cluster/entrypoint.sh"]
CMD ["join_or_bootstrap"]
