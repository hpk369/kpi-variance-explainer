#!/usr/bin/env bash
# Upload the Space app to Hugging Face.
# Usage: bash scripts/deploy_space.sh <hf-username>/<space-name>
# Builds space/build/ (the app, the agent and what it imports, config, documents, recorded answers) and uploads it
# with the hf CLI (run `hf auth login` once first). Secrets (.env) are never copied: set them in the Space settings.
set -euo pipefail
REPO="${1:?usage: bash scripts/deploy_space.sh <hf-username>/<space-name>}"
cd "$(dirname "$0")/.."

if [ ! -f space/cache.json ]; then
  echo "space/cache.json is missing: run python space/build_cache.py first (the Space needs it for the fallback)." >&2
  exit 1
fi

rm -rf space/build && mkdir -p space/build/rag
cp space/app.py space/core.py space/fallback.py space/smoke_test.py space/cache.json \
   space/requirements.txt space/README.md space/build/
cp -R agent common config knowledge space/build/
cp rag/__init__.py rag/chunking.py rag/retrieval_tests.py space/build/rag/   # keyword search if Vector Search is down
find space/build -name "__pycache__" -prune -exec rm -rf {} +
find space/build -name ".env*" -delete

python3 -c "import json; d=json.load(open('space/build/cache.json')); print(f\"cache.json: {len(d['questions'])} recorded answers, data as of {d['as_of']}\")"
echo "Uploading to https://huggingface.co/spaces/$REPO"
hf upload "$REPO" space/build . --repo-type space --commit-message "Deploy $(date '+%Y-%m-%d %H:%M')"
echo "Done. Watch the build in the Space's App tab."
