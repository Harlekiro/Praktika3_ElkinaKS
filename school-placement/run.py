#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Точка входа приложения.

Из корня репозитория:

    .\\.venv\\Scripts\\python.exe school-placement\\run.py

Из папки school-placement:

    ..\\.venv\\Scripts\\python.exe run.py
"""

from school_app.server import main

if __name__ == "__main__":
    raise SystemExit(main())
