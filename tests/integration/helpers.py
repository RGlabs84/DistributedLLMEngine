"""
What the scenario modules share: building a world, and reading the wires.

Each scenario module owns one world (a module-scoped fixture) and tears it down when its last test is done, so only one
world's processes (a coordinator, its llama-server and up to three members with theirs) are alive at a time.
"""

import json
import secrets
from pathlib import Path
from typing import Any, Dict, List

from tests.integration import checks
from tests.integration.world import World

FORBIDDEN_AT_MEMBER = ("authorization", "x-api-key", "cookie", "x-forwarded-for", "x-real-ip", "x-dlne-session",
                       "x-dlne-privacy", "x-dlne-route", "x-dlne-ticket", "origin", "referer")
PUBLIC_REPO = "dlne-it/open-demo"


def build(run_root: Path, resources, name: str, members: List[Dict[str, Any]], *, trusted: List[str] = (), start_later=(),
          ready: int = None, role_grace: float = 3.0, **world_kw) -> World:
    """A running world: the coordinator, then each member (a dict of add_member arguments), waiting until ``ready``
    members are offered and the coordinator's own engine runs. ``role_grace`` shortens the coordinator's 90 s wait
    before it gives a worker nobody has measured a coding job (None keeps 90)."""
    world = World(run_root / name, resources, **world_kw)
    for spec in members:
        spec = dict(spec)
        world.add_member(spec.pop("name"), **spec)
    try:
        world.start_coordinator(trusted=[world.members[n].trust_id for n in trusted], role_grace=role_grace)
        for member_name in world.members:
            if member_name not in start_later:
                world.start_member(member_name)
        world.wait_members_ready(ready if ready is not None else len(world.members) - len(start_later))
        world.wait_engine_ready()
    except BaseException:
        print(world.diagnostics())
        world.stop()
        raise
    return world


def hits(world: World) -> Dict[str, int]:
    """Chat / completion requests each member has received so far (read off the wire in front of the member)."""
    return {name: len(member.completions()) for name, member in world.members.items()}


def served_by(before: Dict[str, int], after: Dict[str, int]) -> List[str]:
    return sorted(name for name in after if after[name] > before.get(name, 0))


def posts_since(world: World, count: int) -> List[Dict[str, Any]]:
    return checks.chat_posts(world.client_tap.snapshot())[count:]


def post_count(world: World) -> int:
    return len(checks.chat_posts(world.client_tap.snapshot()))


def route_lookups(world: World) -> int:
    return sum(1 for conn in world.client_tap.snapshot()
               for line, _h, _b in checks.requests_on(conn) if line.startswith("GET /v1/coding/route"))


def all_wire_bytes(world: World, since: int = 0) -> bytes:
    return b"".join(bytes(c.c2s) + bytes(c.s2c) for c in world.client_tap.snapshot()[since:])


def wire_summary(world: World, since: int = 0) -> str:
    """One line per request the editor's wire carried (for failure messages): the request line and the answer's first line."""
    lines = []
    for conn in world.client_tap.snapshot()[since:]:
        for request_line, headers, body in checks.requests_on(conn):
            route = f" route={headers['x-dlne-route'][:8]}" if headers.get("x-dlne-route") else ""
            lines.append(f"{request_line.split(' HTTP')[0]}{route} body={len(body)}B")
        lines.append("   -> " + bytes(conn.s2c)[:120].decode("latin-1").replace("\r\n", " | "))
    return "\n".join(lines)


def make_workspace(root: Path, remote: str) -> Path:
    folder = root / f"workspace-{secrets.token_hex(3)}"
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "config").write_text(f'[core]\n\trepositoryformatversion = 0\n[remote "origin"]\n\turl = {remote}\n')
    return folder


def open_mode_kwargs(world: World) -> Dict[str, Any]:
    """Client settings that make the extension work out privacy mode `open`: a workspace whose git remote is a public
    repository (the one answer that would come from api.github.com is given by the driver; nothing leaves the machine)."""
    return {"workspace": make_workspace(world.root, f"https://github.com/{PUBLIC_REPO}.git"),
            "fake_github": {f"/repos/{PUBLIC_REPO}": {"full_name": PUBLIC_REPO, "private": False, "visibility": "public"}}}


def prompt_with(marker: str) -> str:
    return f"Write one short Python comment that includes the exact word {marker}, then a one line function."


def assert_member_traffic_sealed(world: World) -> int:
    """Over everything a world carried: every request the editor sent with a route is only an envelope, every answer the
    coordinator marked as served by a member is only sealed frames, and nothing the members relayed is readable.
    Returns how many member-served answers were checked."""
    checked = 0
    for post in checks.chat_posts(world.client_tap.snapshot()):
        if post["headers"].get("x-dlne-route"):
            assert checks.envelope(post["body"]) is not None, f"a request with a route was not an envelope: {post['body'][:120]!r}"
        if post["answer_headers"].get("x-dlne-served") == "member":
            checked += 1
            body = post["answer_body"]
            assert b'"choices"' not in body and b'"content"' not in body and b'"delta"' not in body, body[:200]
            for line in checks.sse_data_lines(body):
                assert line == "[DONE]" or list(json.loads(line)) in (["dlne_e2e"], ["dlne"]), line[:120]
    for member in world.members.values():
        for post in checks.chat_posts(member.connections()):
            assert checks.envelope(post["body"]) is not None, f"member {member.name} was sent a plain body: {post['body'][:120]!r}"
    return checked


def assert_nothing_readable(world: World, needles: List[str], *, since: int = 0, members: bool = True) -> None:
    """The editor-to-coordinator wire, the coordinator's logs and files, and the tunnel in front of every member hold none
    of the needles; neither do the members' own logs (no-trace)."""
    problems = []
    wire = all_wire_bytes(world, since)
    for hit in checks.plaintext_found(wire, needles):
        problems.append(f"found on the editor-to-coordinator wire: {hit!r}")
    for hit in checks.plaintext_found(world.logs_text().encode("utf-8", "replace"), needles):
        problems.append(f"found in the coordinator's logs: {hit!r}")
    for hit in checks.plaintext_found(world.coordinator_disk_bytes(), needles):
        problems.append(f"found in a file in the coordinator's folder: {hit!r}")
    if members:
        for member in world.members.values():
            blob = b"".join(bytes(c.c2s) + bytes(c.s2c) for c in member.connections())
            for hit in checks.plaintext_found(blob, needles):
                problems.append(f"found on the tunnel in front of member {member.name}: {hit!r}")
            for hit in checks.plaintext_found((member.log() + member.llama_log()).encode("utf-8", "replace"), needles):
                problems.append(f"found in member {member.name}'s logs: {hit!r}")
    assert not problems, "\n".join(problems)
