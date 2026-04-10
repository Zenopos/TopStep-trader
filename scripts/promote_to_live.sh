#!/bin/bash

set -e

# Warning banner
echo "============================================================"
echo "⚠️  PROMOTING TO LIVE FUNDED ACCOUNT"
echo "============================================================"
echo ""
echo "THIS IS A CRITICAL ACTION"
echo "Trading with real money will commence."
echo ""
echo "Type 'I UNDERSTAND THE RISKS' to proceed:"
read -r CONFIRMATION

if [ "$CONFIRMATION" != "I UNDERSTAND THE RISKS" ]; then
    echo "Confirmation not matched. Aborting."
    exit 1
fi

echo ""
echo "Running pre-flight checks..."
echo ""

# CHECK 1: TRADOVATE_ENV must equal "live"
echo -n "CHECK 1: TRADOVATE_ENV = 'live' ... "
if [ "$TRADOVATE_ENV" = "live" ]; then
    echo "✅ PASS"
else
    echo "❌ FAIL (TRADOVATE_ENV is '$TRADOVATE_ENV', expected 'live')"
    exit 1
fi

# CHECK 2: TOPSTEP_DAILY_LOSS_LIMIT must be set and negative
echo -n "CHECK 2: TOPSTEP_DAILY_LOSS_LIMIT set & negative ... "
if [ -n "$TOPSTEP_DAILY_LOSS_LIMIT" ] && [ "$TOPSTEP_DAILY_LOSS_LIMIT" -lt 0 ] 2>/dev/null; then
    echo "✅ PASS"
else
    echo "❌ FAIL (TOPSTEP_DAILY_LOSS_LIMIT not set or not negative)"
    exit 1
fi

# CHECK 3: TOPSTEP_MAX_CONTRACTS must be <= 3
echo -n "CHECK 3: TOPSTEP_MAX_CONTRACTS <= 3 ... "
if [ -n "$TOPSTEP_MAX_CONTRACTS" ] && [ "$TOPSTEP_MAX_CONTRACTS" -le 3 ] 2>/dev/null; then
    echo "✅ PASS"
else
    echo "❌ FAIL (TOPSTEP_MAX_CONTRACTS is '$TOPSTEP_MAX_CONTRACTS', must be <= 3)"
    exit 1
fi

# CHECK 4: Backtest report file must exist
echo -n "CHECK 4: Backtest report exists ... "
if [ -f "backtest/results/latest.json" ]; then
    echo "✅ PASS"
else
    echo "❌ FAIL (backtest/results/latest.json not found)"
    exit 1
fi

# CHECK 5: Git working directory must be clean
echo -n "CHECK 5: Git working directory clean ... "
if git diff --quiet && git diff --cached --quiet; then
    echo "✅ PASS"
else
    echo "❌ FAIL (Uncommitted changes detected)"
    exit 1
fi

echo ""
echo "All pre-flight checks passed! ✅"
echo ""
echo "Starting Topstep Bot in LIVE mode..."
echo "========================================"

# Run docker-compose
docker-compose up --build topstep_bot
