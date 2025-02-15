#!/usr/bin/env /bin/sh

/filebrowser config init \
    --address="0.0.0.0" \
    --branding.disableExternal \
    --branding.files="/branding" \
    --branding.name="AntiBrowser" \
    --locale="ru" \
    --perm.create="false" \
    --perm.delete="false" \
    --perm.download="false" \
    --perm.execute="false" \
    --perm.modify="true" \
    --perm.rename="false" \
    --perm.share="false" \
    --port="80" \
    --root="/srv" \
    --shell="sh -c" \
    --singleClick

/filebrowser cmds add after_save '/hooks/doall.sh'

/filebrowser users add --lockPassword \
    ${FILEBROWSER_USERNAME:-"admin"}  \
    ${FILEBROWSER_PASSWORD:-"password"} 

exec /filebrowser