#!/bin/zsh
cd "/Users/maxli/workspace/ai-digest"
"/Users/maxli/workspace/ai-digest/.venv/bin/python" main.py && "/Users/maxli/workspace/ai-digest/.venv/bin/python" scripts/generate_report.py daily
