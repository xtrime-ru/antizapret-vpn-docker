FROM filebrowser/filebrowser:v2.63.23
# Keep access to the existing root-owned bind mount and update hook.
USER root
COPY . .
HEALTHCHECK --start-period=2s --interval=5s --timeout=3s CMD wget -q --spider http://127.0.0.1:80/health || exit 1
ENTRYPOINT [ "/init.sh" ]
