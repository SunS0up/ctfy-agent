#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""允许 python -m ctfy_agent <command>。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
