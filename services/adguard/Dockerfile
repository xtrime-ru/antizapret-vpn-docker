FROM adguard/adguardhome:latest

RUN apk --no-cache add iptables apache2-utils yq jq bash bind-tools curl

COPY --from=xtrime/antizapret-vpn:6.7.8  /usr/bin/routes /usr/bin/routes

ADD . /root/

ENTRYPOINT ["/root/entrypoint.sh"]

CMD [ \
	"--no-check-update", \
	"-c", "/opt/adguardhome/conf/AdGuardHome.yaml", \
	"-w", "/opt/adguardhome/work" \
]