#!/usr/bin/env bash
set -euo pipefail

echo "[1/4] Installing system packages..."
sudo apt update
sudo apt install -y python3 python3-venv python3-pip antiword libreoffice

echo "[2/4] Creating virtual environment..."
python3 -m venv .venv

echo "[3/4] Installing Python packages..."
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "[4/4] Done."
echo "Next:"
echo "  cp .env.example .env"
echo "  nano .env"
echo "  .venv/bin/python resume_pipeline.py --check"
