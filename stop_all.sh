#!/bin/bash
if [ -f /tmp/smartaccess_pids.txt ]; then
    echo "Stopping all SmartAccess IoT services..."
    while read -r SENSOR RELAY ALERT VOICE; do
        kill $SENSOR $RELAY $ALERT $VOICE 2>/dev/null
    done < /tmp/smartaccess_pids.txt
    rm -f /tmp/smartaccess_pids.txt
    echo "All services stopped."
else
    echo "No running services found."
fi
