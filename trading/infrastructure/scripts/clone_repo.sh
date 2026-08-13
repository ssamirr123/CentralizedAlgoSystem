#!/bin/bash
# Clones the repo onto the instance at ~/trading-app, on the branch that
# actually has trading/ (main doesn't have it yet -- this branch hasn't
# been merged). Bash equivalent of clone_repo.ps1 for Linux instances.
set -e

TARGET_DIR="$HOME/trading-app"
REPO_URL="https://github.com/ssamirr123/CentralizedAlgoSystem.git"
BRANCH="web-base-algo-trading-control"

if [ -d "$TARGET_DIR" ]; then
    echo "$TARGET_DIR already exists -- pulling latest instead of cloning."
    cd "$TARGET_DIR"
    git fetch origin
    git checkout "$BRANCH"
    git pull origin "$BRANCH"
else
    git clone --branch "$BRANCH" "$REPO_URL" "$TARGET_DIR"
fi

echo "Done."
