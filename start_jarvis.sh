#!/usr/bin/env bash
cd "$(dirname "$0")"
python3 jarvis.py || python3 jarvis.py --text
