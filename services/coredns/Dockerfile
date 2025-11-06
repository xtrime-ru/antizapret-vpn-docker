FROM ubuntu:24.04

WORKDIR /
COPY --from=coredns/coredns:1.13.1 /etc/ssl/certs/ /etc/ssl/certs/
COPY --from=coredns/coredns:1.13.1 /coredns /coredns

RUN apt update -y && apt install -y gettext-base dnsutils

COPY . /root/

ENTRYPOINT ["/root/entrypoint.sh"]