#!/bin/bash
# start.sh — entrypoint
#
# 1. Decompress any *.csv.gz in $INPUT_DIR if the .csv isn't already present
#    (the repo ships only the gzipped CSV to stay under git/GitHub size limits).
# 2. Exec uvicorn. The app reads DATABASE_URL from env (compose service or
#    Render managed DB).

set -e

INPUT_DIR_RESOLVED="${INPUT_DIR:-input_files}"
if [ -d "$INPUT_DIR_RESOLVED" ]; then
    for gz in "$INPUT_DIR_RESOLVED"/*.csv.gz; do
        [ -e "$gz" ] || continue          # no matches → skip
        csv="${gz%.gz}"
        if [ ! -f "$csv" ]; then
            echo "[start] decompressing $gz -> $csv"
            gunzip -k "$gz"
        fi
    done
fi

echo "[start] launching uvicorn on port ${PORT:-8000}"
exec uvicorn backend:app --host 0.0.0.0 --port "${PORT:-8000}"
