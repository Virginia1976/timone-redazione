#!/bin/bash
cd "$(dirname "$0")"
exec caffeinate -i .venv/bin/python app.py
