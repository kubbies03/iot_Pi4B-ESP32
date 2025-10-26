#!/bin/bash
# SmartAccess IoT — chạy toàn bộ services một lệnh
cd /home/pi/venvs/iot
source bin/activate

echo "=== SmartAccess IoT starting ==="

# Mỗi service chạy nền (background)
python sensor_service.py &
SENSOR_PID=$!
echo "sensor_service PID=$SENSOR_PID"

python relay_service.py &
RELAY_PID=$!
echo "relay_service PID=$RELAY_PID"

python alert_service.py &
ALERT_PID=$!
echo "alert_service PID=$ALERT_PID"

python voice_service.py &
VOICE_PID=$!
echo "voice_service PID=$VOICE_PID"

# Ghi PID ra file để dễ kill
echo "$SENSOR_PID $RELAY_PID $ALERT_PID $VOICE_PID" > /tmp/smartaccess_pids.txt
echo "All services started. Use ./stop_all.sh to stop."
wait
