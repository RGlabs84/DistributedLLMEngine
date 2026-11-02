"""
Who answers a phone's (or the web chat's) message first (2.9.0, docs/release-2.9-contract.md 6b and 6e).

``PHONE_ROUTE`` (engine/config.py PHONE_ROUTES) picks the order between TheWall's phone rung and the volunteers' CPU pool:

    wall_first        (default) TheWall's phone rung if a lane is free; all lanes busy: a text goes to a ready volunteer
    volunteers_first  a ready volunteer (fast enough while TheWall's lane is free), TheWall's lane as the backup
    wall_only         today's order: volunteers answer only while TheWall's engine cannot

Photos never go to a volunteer in any of them. This module only decides; the coordinator (``_wall_phone_rung``, the one
seam to the phone engine) says what TheWall's rung is right now, and answers.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

from engine.config import PHONE_ROUTES

WALL = "wall"                # TheWall's phone rung: wait for a lane, the usual 429 when none comes
VOLUNTEERS = "volunteers"    # a ready volunteer CPU now
POOL = "pool"                # the CPU pool as it has always been asked when TheWall's rung cannot answer at all

DEFAULT_ROUTE = "wall_first"


@dataclass(frozen=True)
class WallRung:
    """
    What TheWall's phone rung is right now (coordinator._wall_phone_rung; builder L's phone engine answers the same
    question):

    - ``available``: it can take a phone message at all (a running engine with phone lanes, not swapping);
    - ``free``: a lane is free this moment, nobody is waiting for one;
    - ``reads_photos``: it can read a photo (only worked out when asked for photos; False otherwise);
    - ``engine``: what the answering function needs of it (the manager's snapshot: port, phone_slots, dynamic_slots,
      phone_context), None when not available.
    """
    available: bool
    free: bool = False
    reads_photos: bool = False
    engine: Optional[Dict[str, Any]] = None


def clean_route(value: Any) -> str:
    """The configured route, or the default for anything not one of PHONE_ROUTES."""
    return value if isinstance(value, str) and value in PHONE_ROUTES else DEFAULT_ROUTE


def choose(route: str, photos: bool, wall: WallRung, volunteer_free: bool, volunteer_fast: bool) -> str:
    """
    WALL, VOLUNTEERS or POOL for one message.

    ``volunteer_free``: a ready volunteer has a free slot; ``volunteer_fast``: and its measured speed is at least
    PHONE_MIN_TPS (or not measured yet). A photo is TheWall's or nobody's (POOL then means the coordinator's "photos are
    not available right now" answer, never a volunteer). With TheWall's rung unavailable the CPU pool answers, as before.
    """
    route = clean_route(route)
    if photos:
        return WALL if wall.available else POOL
    if not wall.available:
        return POOL
    if route == "wall_only":
        return WALL
    if route == "wall_first":
        return WALL if wall.free or not volunteer_free else VOLUNTEERS
    # volunteers_first: while TheWall's lane is free a slow volunteer is skipped; with it busy any free volunteer will do
    if wall.free:
        return VOLUNTEERS if volunteer_fast else WALL
    return VOLUNTEERS if volunteer_free else WALL
