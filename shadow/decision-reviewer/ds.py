"""Entry point: `uv run --no-project python shadow/decision-reviewer/ds.py <cmd> ...`

stub/kev/clm need only the standard library. The laya backend needs the laya package:
`uv run --no-project --with laya==0.3.26 python shadow/decision-reviewer/ds.py ...`
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from decision_shadow.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
