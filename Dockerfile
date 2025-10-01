FROM mysql:8.4-oracle

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
    wget https://releases.hashicorp.com/consul/1.21.5/consul_1.21.5_linux_amd64.zip -O /tmp/consul.zip && \
    echo "2dfb63fcabe9f15b956cf408248d9ebe36cfd662ca182352942a3bd4e5d5faca /tmp/consul.zip" | sha256sum -c && \
    unzip /tmp/consul.zip -d /usr/local/bin && \
    rm /usr/local/bin/LICENSE.txt && \
    rm /tmp/consul.zip && \
    # Install ProxySQL
    wget https://github.com/sysown/proxysql/releases/download/v3.0.2/proxysql-3.0.2-1-centos9.x86_64.rpm -O /tmp/proxysql.rpm && \
    echo "b94d24dc7e4608e3b2006a43d7a1b112143d3fe108baee5136db1f7341d3aedf /tmp/proxysql.rpm" | sha256sum -c && \
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
