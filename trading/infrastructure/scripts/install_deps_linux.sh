#!/bin/bash
# Installs git + python3/pip if missing. Handles both Amazon Linux
# (yum/dnf) and Ubuntu/Debian (apt) since we don't assume which AMI this
# runs -- check first, only install what's actually missing.
set -e

echo "=== Checking existing tools ==="
command -v git >/dev/null 2>&1 && echo "git: $(git --version)" || echo "git: NOT FOUND"
command -v python3 >/dev/null 2>&1 && echo "python3: $(python3 --version)" || echo "python3: NOT FOUND"
command -v pip3 >/dev/null 2>&1 && echo "pip3: $(pip3 --version)" || echo "pip3: NOT FOUND"

NEED_GIT=$(command -v git >/dev/null 2>&1 && echo "no" || echo "yes")
NEED_PYTHON=$(command -v python3 >/dev/null 2>&1 && echo "no" || echo "yes")
NEED_PIP=$(command -v pip3 >/dev/null 2>&1 && echo "no" || echo "yes")

if [ "$NEED_GIT" = "no" ] && [ "$NEED_PYTHON" = "no" ] && [ "$NEED_PIP" = "no" ]; then
    echo "=== Nothing to install ==="
    exit 0
fi

echo "=== Installing missing tools ==="
if command -v dnf >/dev/null 2>&1; then
    PKG_MGR="dnf"
elif command -v yum >/dev/null 2>&1; then
    PKG_MGR="yum"
elif command -v apt-get >/dev/null 2>&1; then
    PKG_MGR="apt-get"
else
    echo "ERROR: no supported package manager found (tried dnf, yum, apt-get)"
    exit 1
fi
echo "Using package manager: $PKG_MGR"

PACKAGES=""
[ "$NEED_GIT" = "yes" ] && PACKAGES="$PACKAGES git"
[ "$NEED_PYTHON" = "yes" ] && PACKAGES="$PACKAGES python3"
[ "$NEED_PIP" = "yes" ] && PACKAGES="$PACKAGES python3-pip"

if [ "$PKG_MGR" = "apt-get" ]; then
    sudo apt-get update -y
    sudo apt-get install -y $PACKAGES
else
    sudo "$PKG_MGR" install -y $PACKAGES
fi

echo "=== Verifying ==="
git --version
python3 --version
pip3 --version

echo "=== Setting up venv (AL2023's pip-managed requests/urllib3 conflict with"
echo "    RPM-installed ones, so app deps go in a venv, not system site-packages) ==="
REPO_DIR="$HOME/trading-app"
if [ ! -d "$REPO_DIR" ]; then
    echo "ERROR: $REPO_DIR not found -- run clone_repo.sh first"
    exit 1
fi
cd "$REPO_DIR"
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
venv/bin/pip install -r trading/algos/example_strategy/requirements.txt
echo "Done."
