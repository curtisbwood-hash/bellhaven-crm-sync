#!/bin/bash
# Local daily run (alternative to GitHub Actions): scrape -> match -> serve review queue.
# Cron example:  0 8 * * *  cd ~/workspace/bellhaven-sync && ./run_daily.sh
set -e
cd "$(dirname "$0")"
set -a && . ./.env && set +a
python3 scraper.py
python3 matcher.py
echo "Proposals rebuilt. Review at: python3 review_app.py --port 8765"
