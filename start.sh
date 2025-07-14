#!/bin/bash
set -e

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create temp directory
mkdir -p temp_downloads

# Start the application
gunicorn app:app --workers 4 --bind 0.0.0.0:$PORT
