#!/bin/bash
set -e

echo "🚀 Starting deployment..."

# Cleanup previous run
if [ -d "bidding-data" ]; then
    echo "Cleaning up old directory..."
    rm -rf bidding-data
fi

# Clone code
echo "📦 Cloning repository..."
git clone https://github.com/abner20953/bidding-data.git
cd bidding-data

# Install dependencies
echo "📦 Installing dependencies..."
pip install -r requirements.txt

# Start application
echo "🚀 Starting Gunicorn..."
exec gunicorn -w 1 -b 0.0.0.0:8080 --timeout 120 dashboard.app:app
