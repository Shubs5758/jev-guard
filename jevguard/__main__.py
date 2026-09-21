"""Allow ``python -m jevguard`` as well as the ``jevguard`` console script."""

from jevguard.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
