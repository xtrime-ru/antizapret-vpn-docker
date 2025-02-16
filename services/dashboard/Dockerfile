FROM jitesoft/lighttpd
RUN apk add apache2-utils jq
COPY files/www/. /var/www/html
COPY files/init.sh /init.sh
COPY files/entrypoint.sh /entrypoint.sh
ENTRYPOINT [ "/entrypoint.sh" ]