ARG MYSQL_VERSION=8.4.13

# ---- Builder: compile Python dependencies only ----
FROM mysql:${MYSQL_VERSION}-oracle AS pybuild
SHELL ["/bin/bash", "-c"]

ENV PYTHONUSERBASE=/cluster/.prefix
RUN microdnf install -y --nodocs --setopt=install_weak_deps=0 python3.14-devel python3.14-pip gcc && \
    microdnf clean all

COPY requirements.txt .
RUN python3.14 -m pip install --no-cache-dir --user -r requirements.txt

# ---- Final image ----
FROM mysql:${MYSQL_VERSION}-oracle

ENV MYSQL_VERSION=${MYSQL_VERSION}
SHELL ["/bin/bash", "-c"]

RUN \
    # Install dependencies
    microdnf install -y --nodocs --setopt=install_weak_deps=0 \
        libev \
        lz4 \
        # ProxySQL requires Perl. Only the interpreter is installed, as the full perl package also
        # pulls in development tools and another version of Python.
        perl-interpreter \
        perl-DBI \
        perl-DBD-MySQL \
        procps \
        python3.14 \
        rsync \
        unzip \
        wget && \
    # Install Percona XtraBackup
    wget https://downloads.percona.com/downloads/Percona-XtraBackup-8.4/Percona-XtraBackup-8.4.0-7/binary/redhat/9/x86_64/percona-xtrabackup-84-8.4.0-7.1.el9.x86_64.rpm -O /tmp/xtrabackup.rpm && \
    rpm -i /tmp/xtrabackup.rpm && \
    rm /tmp/xtrabackup.rpm && \
    # Install Consul CLI
    wget https://releases.hashicorp.com/consul/1.22.7/consul_1.22.7_linux_amd64.zip -O /tmp/consul.zip && \
    echo "fe25cecd8dd3552a8e5b0941cde1d79bb6004eac384aa45679dd1398f947201d /tmp/consul.zip" | sha256sum -c && \
    unzip /tmp/consul.zip -d /usr/local/bin && \
    rm /usr/local/bin/LICENSE.txt && \
    rm /tmp/consul.zip && \
    # Install ProxySQL
    wget https://github.com/sysown/proxysql/releases/download/v3.1.11/proxysql-3.1.11-1-centos9.x86_64.rpm -O /tmp/proxysql.rpm && \
    echo "c996543ea074a1fa6824e0c68a37bb4a878abb6e11979daeb3dea8962962fccb /tmp/proxysql.rpm" | sha256sum -c && \
    rpm -i /tmp/proxysql.rpm && \
    rm /tmp/proxysql.rpm && \
    # Create directories for Cluster Manager and snapshot
    mkdir /snapshots && \
    mkdir /cluster && \
    # Remove build-time tooling so it does not ship in the image
    # (unzip is left in place — the base image's zip package depends on it)
    microdnf remove -y wget && \
    # Clean up
    microdnf clean all && \
    rm -rf /var/cache/yum/* && \
    rm -rf /var/lib/rpm/__db* && \
    rm -rf /tmp/*

# Install Cluster Manager and dependencies, and set up volume
WORKDIR /cluster

# Copy pre-built Python dependencies from the builder stage
ENV PYTHONUSERBASE=/cluster/.prefix
COPY --from=pybuild /cluster/.prefix /cluster/.prefix

COPY src/mcm ./mcm
COPY src/main.py .
COPY entrypoint.sh .

VOLUME /snapshots
EXPOSE 6033/tcp
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=1m --start-interval=1s \
    CMD ["/cluster/main.py", "--log-level", "WARNING", "healthcheck"]
ENTRYPOINT ["/cluster/entrypoint.sh"]
CMD ["join_or_bootstrap"]
