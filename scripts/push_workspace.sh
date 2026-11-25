#!/usr/bin/env bash
# Upload the code the Databricks job needs to /Workspace/Users/<you>/northlake-cib.
# Usage: bash scripts/push_workspace.sh            (profile "northlake")
#        bash scripts/push_workspace.sh myprofile
# Only code folders go up (pipeline, generator/*.py, common, config, tools, agent, knowledge, rag, notebooks, and later eval):
# never .env, .venv or data files.
set -euo pipefail
PROFILE="${1:-northlake}"
cd "$(dirname "$0")/.."

USER_NAME=$(databricks current-user me -p "$PROFILE" -o json | python3 -c "import sys, json; print(json.load(sys.stdin)['userName'])")
TARGET="/Workspace/Users/$USER_NAME/northlake-cib"
# pipeline/job.json is a template (no personal details in the public repo): fill in the user for this workspace
mkdir -p build
sed "s|{{DATABRICKS_USER}}|$USER_NAME|g" pipeline/job.json > build/job.json

rm -rf build/workspace && mkdir -p build/workspace/generator
for d in pipeline common config tools agent eval knowledge rag notebooks; do   # later phases add folders; missing ones are skipped
  if [ -d "$d" ]; then cp -R "$d" build/workspace/; fi
done
cp generator/*.py build/workspace/generator/
find build/workspace -name "__pycache__" -prune -exec rm -rf {} +

databricks workspace mkdirs "$TARGET" -p "$PROFILE"
databricks workspace import-dir build/workspace "$TARGET" --overwrite -p "$PROFILE"
echo
echo "Uploaded to $TARGET"
echo "Job definition for this workspace: build/job.json"
databricks workspace list "$TARGET/pipeline" -p "$PROFILE"
