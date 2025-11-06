FROM adguard/adguardhome:latest

RUN apk --no-cache add apache2-utils yq bash bind-tools

ADD . /root/

ENTRYPOINT ["/root/entrypoint.sh"]

CMD [ \
	"--no-check-update", \
	"-c", "/opt/adguardhome/conf/AdGuardHome.yaml", \
	"-w", "/opt/adguardhome/work" \
]