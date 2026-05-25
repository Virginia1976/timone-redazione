#!/bin/bash
cd "$(dirname "$0")"
exec caffeinate -i python3 app.py
