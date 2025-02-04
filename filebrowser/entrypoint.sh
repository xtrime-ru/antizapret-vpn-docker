#!/bin/sh
/filebrowser config init \
    --branding.name "AntiZapret" \
    --branding.files "/app/branding" \
    --shell="sh -c" \
    --locale="ru" \
    --lockPassword="true" \
    --perm.create="false" \
    --perm.delete="false" \
    --perm.download="false" \
    --perm.execute="false" \
    --perm.modify="true" \
    --perm.rename="false" \
    --perm.share="false"

/filebrowser cmds add after_save '/app/helpers/calldoall.sh'
/filebrowser users add ${FILEBROWSER_USERNAME:-"admin"} ${FILEBROWSER_PASSWORD:-"password"} --lockPassword
/filebrowser