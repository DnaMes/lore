"""Pre-rebrand entry point: forwards to ``annuli_session_cli``.

The host ``lore-sync.service`` runs ``lore_cli.py`` from the checkout, so this file must stay.
"""

import sys

from annuli_session_cli import main

if __name__ == "__main__":
    sys.exit(main())
