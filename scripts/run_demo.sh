#!/bin/bash

set -e

echo "========================================"
echo "Starting Topstep Bot in DEMO mode"
echo "========================================"

# Check if .env file exists
if [ ! -f ".env" ]; then
    echo "Error: .env file not found!"
    echo "Please create .env file from .env.example"
    exit 1
fi

# Check if TRADOVATE_ENV is set to demo
if [ "$TRADOVATE_ENV" = "live" ]; then
    echo "Error: TRADOVATE_ENV is set to 'live'!"
    echo "Use scripts/promote_to_live.sh for live trading"
    exit 1
fi

# Build and run the container
docker-compose up --build topstep_bot

# Tail logs
docker-compose logs -f topstep_bot
