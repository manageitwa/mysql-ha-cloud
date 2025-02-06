FROM mysql:8.0.41-bookworm

SHELL ["/bin/bash", "-c"]

RUN \
    #
    # Install system basics
    #
    apt-get update && \
    apt-get install -y unzip curl wget gnupg2 lsb-release procps && \
    #
    # Install Percona XtraBackup
    #
    apt-get install -y libdbd-mysql-perl libcurl4-openssl-dev rsync libev4 lz4 && \
    wget https://downloads.percona.com/downloads/Percona-XtraBackup-8.0/Percona-XtraBackup-8.0.35-32/binary/debian/bookworm/x86_64/percona-xtrabackup-80_8.0.35-32-1.bookworm_amd64.deb -O /tmp/xtrabackup.deb && \ 
    dpkg -i /tmp/xtrabackup.deb && \
    rm /tmp/xtrabackup.deb && \
    #
    # Install Consul
    #
    wget https://releases.hashicorp.com/consul/1.20.2/consul_1.20.2_linux_amd64.zip -O /tmp/consul.zip && \
    echo "1bf7ddf332f02e6e36082b0fdf6c3e8ce12a391e7ec7dafd3237bb12766a7fd5 /tmp/consul.zip" | sha256sum -c && \
    unzip /tmp/consul.zip -d /usr/local/bin && \
    rm /usr/local/bin/LICENSE.txt && \
    rm /tmp/consul.zip && \
    #
    # Install minIO client
    #
    wget https://dl.min.io/client/mc/release/linux-amd64/mc -O /usr/local/bin/mc && \
    chmod +x /usr/local/bin/mc && \
    #
    # Install Python 3
    #
    apt-get install -y python3 python3-dev python3-pip && \
    #
    # Install ProxySQL
    #
    wget https://github.com/sysown/proxysql/releases/download/v2.7.2/proxysql_2.7.2-debian12_amd64.deb -O /tmp/proxysql.deb && \
    echo "e534be5aa64b7807beba89b41c7e52c6003a0326942a6ebc6d4da7e55571f9ef /tmp/proxysql.deb" | sha256sum -c && \
    dpkg -i /tmp/proxysql.deb && \
    rm /tmp/proxysql.deb

WORKDIR /cluster

COPY ./mysql_cluster_manager/requirements.txt .

# Install Python dependencies
RUN pip3 install -r requirements.txt --break-system-packages

COPY ./mysql_cluster_manager/src .
COPY ./entrypoint.sh .

CMD ["bash", "entrypoint.sh"]
EXPOSE 6032/tcp
