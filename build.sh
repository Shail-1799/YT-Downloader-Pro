#!/usr/bin/env bash
set -e

echo "--- Installing system dependencies ---"
apt-get update -qq
apt-get install -y ffmpeg

echo "--- Installing Python dependencies ---"
pip install -r requirements.txt
