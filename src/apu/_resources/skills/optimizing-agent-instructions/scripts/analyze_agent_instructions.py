#!/usr/bin/env python3
"""Run APU's privacy-preserving instruction audit from the bundled skill."""

from __future__ import annotations

import sys

from apu.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["audit", *sys.argv[1:]]))
