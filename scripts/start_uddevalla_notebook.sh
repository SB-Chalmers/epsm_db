#!/bin/sh
# Explore the completed Uddevalla database while the national rebuild runs.
set -eu
ubem_repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ubem_repo"
export PATH="$ubem_repo/.venv/bin:$PATH"
export UBEM_DATABASE_URL='postgresql://ubem_owner:ubem-local-only@127.0.0.1:55432/sweden_ubem?options=-c%20default_transaction_read_only%3Don'
export UBEM_NOTEBOOK_DATASET=uddevalla
export UBEM_DATASET=uddevalla
exec "$ubem_repo/.venv/bin/jupyter" lab --ServerApp.root_dir="$ubem_repo" notebooks/explore_uddevalla.ipynb "$@"
