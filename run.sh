#!/usr/bin/env bash
# Scrape the posts in storage/key_value_stores/default/INPUT.json, then write results.xlsx.
# Usage: ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

INPUT=storage/key_value_stores/default/INPUT.json
if [[ ! -f "$INPUT" ]]; then
  mkdir -p "$(dirname "$INPUT")"
  cp INPUT.example.json "$INPUT"
  echo "Created $INPUT from INPUT.example.json. Put your post URL(s) in it and run ./run.sh again."
  exit 1
fi

source .venv/bin/activate
apify run --purge
python scripts/export_excel.py
