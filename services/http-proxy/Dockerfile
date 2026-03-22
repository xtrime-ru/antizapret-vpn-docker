FROM ubuntu:24.04

RUN apt-get update && \
	apt-get install squid apache2-utils openssl iproute2 iputils-ping iptables gettext-base -y --no-install-recommends && \
	apt-get clean && rm -rf /var/lib/apt/lists/* && \
	mkdir -p /var/log/squid /etc/squid/ssl && \
	chown proxy:proxy /var/log/squid /etc/squid/ssl

COPY squid.conf.template /
COPY init.sh /
COPY routes.sh /
RUN chmod +x /init.sh /routes.sh

EXPOSE 3128/tcp 3129/tcp

CMD ["/init.sh"]
