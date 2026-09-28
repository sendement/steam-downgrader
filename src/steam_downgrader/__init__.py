"""steam-downgrader: roll Steam games back to older builds and keep them there."""

import sys
from pathlib import Path

__version__ = "0.1.0"

# The Arch package ships pure-Python libraries that aren't in the distro repos
# (the solsticegamestudios/steam fork, gevent-eventemitter, segno) in
# /usr/lib/steam-downgrader/vendor, next to site-packages. Located relative to
# this file so it also works for the worker process and the systemd unit.
_VENDOR = Path(__file__).resolve().parents[3] / "steam-downgrader" / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))


def main() -> None:
    from .cli import main as cli_main

    cli_main()
