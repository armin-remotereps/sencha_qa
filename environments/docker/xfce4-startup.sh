#!/bin/bash
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS
eval $(dbus-launch --sh-syntax)
echo "export DBUS_SESSION_BUS_ADDRESS=\"${DBUS_SESSION_BUS_ADDRESS}\"" > /tmp/.dbus_env
exec startxfce4
