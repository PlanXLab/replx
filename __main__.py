"""Allow ``python -m replx ...`` as a fast-startup alias for the ``replx`` script.

On Windows the ``replx.exe`` console-script launcher (installed by pip from
``[project.scripts]``) goes through a zipapp-style entry that adds ~450ms to
every invocation. ``python -m replx`` bypasses that launcher and is noticeably
snappier for users who care about CLI startup latency.
"""

import sys

from replx.cli.app import main

if __name__ == "__main__":
    sys.exit(main())
