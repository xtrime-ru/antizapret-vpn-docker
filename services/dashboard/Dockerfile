FROM jitesoft/lighttpd
RUN apk add apache2-utils jq
RUN sed -i "/mimetype\.assign\s*=\s*(/a \  \".json\"         =>      \"application/json\"," "/etc/lighttpd/conf.d/000-mime.conf"
COPY files/www/. /var/www/html
COPY files/init.sh /init.sh
COPY files/entrypoint.sh /entrypoint.sh
ENTRYPOINT [ "/entrypoint.sh" ]