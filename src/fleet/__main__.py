"""Allow `python -m fleet` as an alternative to the `fleet` script."""

from fleet.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
