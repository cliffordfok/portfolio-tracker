#!/bin/sh
exec /usr/local/bin/python3 \
  /data/portfolio-tracker/scripts/check_yfinance_close.py \
  --provider-script /data/scripts/market_quotes.py \
  "$@"
