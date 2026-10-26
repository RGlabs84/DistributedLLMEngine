# The version of this build of DLNE. This is the one place a version is written.
#
# A node must run exactly the coordinator's version: the heartbeat answers HTTP 426 to any
# other, and the VS Code extension then re-fetches the matching, signed engine. A version
# names exactly one archive, so it is never edited to "re-release" something.
#
# To ship: `python scripts/release.py bump minor|major|patch`, commit, then
# `python scripts/release.py deploy`. See engine/release.py for how a release is built,
# signed and verified. (The line below must stay `VERSION = "x.y.z"`; the tool edits it.)
VERSION = "2.4.0"
