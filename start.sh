#!/bin/bash
set -e
set -x  # Enable debug printing

echo "🚀 Starting deployment..."

# Verify environment
echo "🔍 Checking environment..."
python --version
pip --version
git --version || echo "⚠️ Git is not installed"

# Cleanup previous run
if [ -d "bidding-data" ]; then
    echo "Cleaning up old directory..."
    rm -rf bidding-data
fi

# Clone code
echo "📦 Cloning repository..."
git clone https://github.com/abner20953/bidding-data.git || { echo "❌ Git clone failed"; exit 1; }
cd bidding-data

# Install dependencies
echo "📦 Installing dependencies..."
pip install -r requirements.txt || { echo "❌ Pip install failed"; exit 1; }

# Start application
echo "🚀 Starting Gunicorn..."
# Use 0.0.0.0 to bind to all interfaces
exec gunicorn -w 1 -b 0.0.0.0:8080 --timeout 120 dashboard.app:app
