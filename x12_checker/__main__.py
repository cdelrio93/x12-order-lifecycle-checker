"""Lets the tool run as: python -m x12_checker <folder> [--as-of ...]"""

import sys

from x12_checker.report import main

sys.exit(main())
