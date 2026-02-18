FROM ubuntu:24.04

RUN apt-get update && \
	apt-get install nano dante-server iproute2 iputils-ping iptables gettext-base -y --no-install-recommends && \
	apt-get clean && rm -rf /var/lib/apt/lists/*

COPY danted.conf.template /
COPY init.sh /
COPY routes.sh /

EXPOSE 8118/tcp 8118/udp

CMD ["/init.sh"]
