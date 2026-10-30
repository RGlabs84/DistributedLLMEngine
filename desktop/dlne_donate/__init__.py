"""
DLNE Donate: a small tray app that lends this computer's GPU or CPU to the DLNE swarm.

It does what the VS Code extension's worker manager does (vscode-extension/src/worker/workerManager.ts),
without VS Code: fetch the signed engine release for the coordinator's version, run the worker, show what
the swarm made of this machine. Nothing here talks to anyone but the coordinator and the engine's own
download hosts, and nothing runs that has not been verified against the maintainer's Ed25519 key.
"""

__version__ = "1.0.2"

APP_NAME = "DLNE Donate"
APP_ID = "dlne-donate"

# The public swarm (the old name https://dlne.duckdns.org still answers, but this is DLNE's own address).
COORDINATOR_URL = "https://dlne.rossgilsonlabs.dev"
WEBSITE_URL = "https://dlne.rossgilsonlabs.dev"

# The public network key (the same one the VS Code extension ships): it names the public swarm, it is not a secret.
PUBLIC_NETWORK_KEY = "bd1d9bbf50b93e10a963fa2d0b6ee44e1138ca20f4036b765dbc51dd7bc7f083"
