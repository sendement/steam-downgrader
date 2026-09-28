"""steam-downgrader: roll Steam games back to older builds and keep them there."""

__version__ = "0.1.0"


def main() -> None:
    from .cli import main as cli_main

    cli_main()
