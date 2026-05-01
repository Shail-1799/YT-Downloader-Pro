#!/usr/bin/env bash
set -e

echo "--- Installing Python dependencies ---"
pip install -r requirements.txt

echo "--- Force upgrading yt-dlp to latest ---"
pip install --upgrade yt-dlp

echo "--- Versions ---"
yt-dlp --version

echo "--- Done ---"
