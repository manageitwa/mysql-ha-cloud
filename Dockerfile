ARG MYSQL_VERSION=8.4.7
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
    wget https://downloads.percona.com/downloads/Percona-XtraBackup-8.4/Percona-XtraBackup-8.4.0-4/binary/redhat/9/x86_64/percona-xtrabackup-84-8.4.0-4.1.el9.x86_64.rpm -O /tmp/xtrabackup.rpm && \
    rpm -i /tmp/xtrabackup.rpm && \
    rm /tmp/xtrabackup.rpm && \
    # Install Consul CLI
    wget https://releases.hashicorp.com/consul/1.22.1/consul_1.22.1_linux_amd64.zip -O /tmp/consul.zip && \
    echo "91222c7ec141f1c2c92f6b732eeb0251220337e4c07c768cbc6ae633fef69733 /tmp/consul.zip" | sha256sum -c && \
    unzip /tmp/consul.zip -d /usr/local/bin && \
    rm /usr/local/bin/LICENSE.txt && \
    rm /tmp/consul.zip && \
    # Install ProxySQL
    wget https://github.com/sysown/proxysql/releases/download/v3.0.3/proxysql-3.0.3-1-centos9.x86_64.rpm -O /tmp/proxysql.rpm && \
    echo "6d02e80e9d29e4141e48b5ef733ed762d1957ec0b01dc57e0116bd945a1fe83d /tmp/proxysql.rpm" | sha256sum -c && \
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
