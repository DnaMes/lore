"""Pre-rebrand entry point: forwards to ``annuli_session_cli``.

Scripts and old installs may still run ``lore_session_cli.py`` directly, so this file must stay.
"""

import sys

from annuli_session_cli import main

if __name__ == "__main__":
    sys.exit(main())
