"""``python -m bot.cli.modexctl`` entry point.

Allows the packaged installer's ``modexctl.bat`` shim
(``python.exe -m bot.cli.modexctl %*``) to execute without a console
script entry point — needed because the installer's standalone Python
does not run ``pip install`` to generate ``.exe`` console scripts.
"""

from bot.cli.modexctl.main import main

if __name__ == "__main__":
    main()
