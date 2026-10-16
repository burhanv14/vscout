#!/usr/bin/env python3
import sys
from pathlib import Path

# Ensure workspace root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from oss_agent.cli import main

if __name__ == "__main__":
    main()
