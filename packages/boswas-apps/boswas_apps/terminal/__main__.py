"""python3 -m boswas_apps.terminal: the terminal of Boswas OS. See boswas-terminal(1)."""

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
