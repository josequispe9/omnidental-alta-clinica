"""Entrada del middleware en Vercel (FastAPI). Vercel busca una variable `app` en index.py.

Localmente se sigue corriendo como siempre:  cd middleware && ../.venv/bin/uvicorn app:app --port 8000
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "middleware"))

from app import app  # noqa: E402,F401
