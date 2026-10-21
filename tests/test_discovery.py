import pytest

from .harness import wait_for

pytestmark = pytest.mark.integration


def test_three_peers_discover_each_other(spawn):
    peers = spawn.many("Alpha", "Bravo", "Charlie")
    ids = {p.peer_id for p in peers}
    assert len(ids) == 3, "each peer must have its own peer ID"

    for p in peers:
        others = ids - {p.peer_id}
        wait_for(lambda: others <= p.online_peer_ids(), timeout=30, message=f"{p.name} to see the other two")
        assert p.peer_id not in p.online_peer_ids(), "a peer must not list itself"

    alpha, bravo = peers[0], peers[1]
    listing = {x["peer_id"]: x for x in alpha.get("/api/peers").json()["peers"]}
    assert listing[bravo.peer_id]["name"] == "Bravo"
    assert listing[bravo.peer_id]["port"] == bravo.port
    assert listing[bravo.peer_id]["state"] == "online"
    assert alpha.get("/api/status").json()["peers_online"] >= 2


def test_late_joiner_is_discovered(spawn):
    first, second = spawn.many("First", "Second")
    wait_for(lambda: second.peer_id in first.online_peer_ids(), timeout=30, message="First to see Second")
    late = spawn("Late")
    for p in (first, second):
        wait_for(lambda: late.peer_id in p.online_peer_ids(), timeout=30, message=f"{p.name} to see Late")
    wait_for(lambda: {first.peer_id, second.peer_id} <= late.online_peer_ids(), timeout=30,
             message="Late to see the existing peers")
