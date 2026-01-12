#!/bin/bash
set -e
echo "Starting deployment..."
if [ -d "bidding-data" ]; then
    rm -rf bidding-data
fi
git clone https://github.com/abner20953/bidding-data.git
cd bidding-data
pip install -r requirements.txt
exec gunicorn -w 1 -b 0.0.0.0:8080 --timeout 120 dashboard.app:app
