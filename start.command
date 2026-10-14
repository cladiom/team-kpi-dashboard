#!/bin/bash
# Double-click this file to start the dashboard.
cd "$(dirname "$0")" || exit 1
echo "Starting Weekly Ops Dashboard…"
(sleep 2 && open "http://localhost:8787") &
exec python3 server.py
