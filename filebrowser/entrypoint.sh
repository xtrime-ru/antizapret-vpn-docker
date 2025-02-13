#!/bin/sh
/filebrowser config init \
    --shell="sh -c" \
    --locale="ru" \
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