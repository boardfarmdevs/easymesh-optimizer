"""The room and optimizer acceptance tools' tests. The tools load each other as
siblings; they need the optimizer and the room service (this checkout) and the
medium's configurator, next to it as in the labs (``../medium``)."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent), str(HERE.parents[1] / "medium" / "configurator")]
