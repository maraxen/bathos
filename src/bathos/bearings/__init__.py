"""Bearings: blind packet, reading record and charter comparison (spec 261104, slice 2).

Bathos builds a deterministic packet of primary records for a blind reader, stores the
reader's cited reading, and checks a mapping onto the confirmed charter. It never runs a
reader (spec T3).
"""

from __future__ import annotations
