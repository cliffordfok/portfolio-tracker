#!/bin/sh
exec /usr/local/bin/python3 \
  /data/portfolio-tracker/scripts/run_paper_trader_market_window.py \
  -- /usr/local/bin/python3 /data/scripts/swing_trader.py
