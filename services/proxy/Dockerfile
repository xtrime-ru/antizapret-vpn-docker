FROM caddy:2
RUN apk add openssl
COPY files/entrypoint.sh /entrypoint.sh
COPY files/init.sh /init.sh
ENTRYPOINT [ "/entrypoint.sh" ]