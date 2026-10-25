"""
Where the DLNE Discord bot token comes from.

WHY THIS MODULE EXISTS
----------------------
The token used to be pasted as a string literal into all four scripts in this folder, which
meant it was committed to git — four times — and lived in the repository's history forever.
A bot token is a bearer credential: anyone holding it can post as the bot, read every
channel it can see, and manage whatever the bot has permissions for. Publishing the
repository, adding a collaborator, or flipping it public would hand that over.

So the token is now supplied at run time and never stored in a tracked file:

1. the ``DISCORD_BOT_TOKEN`` environment variable, or
2. ``discord/scripts/.bot_token`` — a one-line file, gitignored, for convenience when
   running these by hand.

Neither can be committed by accident, and rotating the token means changing one place
rather than remembering which four files hold a copy.
"""

import os

TOKEN_ENV_VAR = "DISCORD_BOT_TOKEN"
TOKEN_FILE_NAME = ".bot_token"

_HELP = f"""
No Discord bot token found.

Provide it one of two ways:

  1. Environment variable (preferred):
       PowerShell:  $env:{TOKEN_ENV_VAR} = "<token>"
       bash:        export {TOKEN_ENV_VAR}="<token>"

  2. A local file, ignored by git:
       discord/scripts/{TOKEN_FILE_NAME}   (the token on a single line)

Get or reset the token at https://discord.com/developers/applications -> your app -> Bot.
Never paste it into a tracked file.
""".strip()


def load_token() -> str:
    """
    Return the bot token, or exit with instructions.

    Exits rather than raising because every caller is a top-level script run by hand; a
    traceback would bury the one thing the operator needs to read.
    """
    token = (os.environ.get(TOKEN_ENV_VAR) or "").strip()
    if token:
        return token

    token_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), TOKEN_FILE_NAME)
    try:
        with open(token_path, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError:
        token = ""

    if not token:
        raise SystemExit(_HELP)
    return token
