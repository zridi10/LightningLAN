"""While you wait: the head-to-head games (lightninglan/play.py)."""

import json
from types import SimpleNamespace

import pytest

from lightninglan import play
from lightninglan.auth import Authenticator
from lightninglan.play import (
    Arcade, PlayError, interleave, parse_record, replay_board, replay_rounds, seal,
)

from .conftest import LAN_CLIENT
from .harness import wait_for

A_ID, B_ID, C_ID = "a" * 32, "b" * 32, "c" * 32
UI = {"X-LightningLAN-Client": "ui"}


# ----- the rules -----------------------------------------------------------------------------------

def test_moves_count_only_in_turn():
    assert interleave([0, 1, 2], [4, 5]) == [0, 4, 1, 5, 2]
    assert interleave([0, 1, 2, 3], [4]) == [0, 4, 1]  # the first player can't move twice in a row
    assert interleave([0], [4, 5, 6]) == [0, 4]


def test_tictactoe_wins_and_draws():
    board = replay_board(False, [0, 1, 2], [4, 5])
    assert board.winner == 0 and board.line == (0, 1, 2) and board.legal() == []
    board = replay_board(False, [0, 2, 3, 7, 8], [1, 4, 5, 6])
    assert board.draw and board.winner is None
    board = replay_board(False, [0, 0], [4])  # an illegal move stops the replay
    assert board.cells.count("x") == 1 and board.turn == 0


def test_ultimate_sends_you_to_the_board_your_opponent_picked():
    board = replay_board(True, [], [])
    assert len(board.legal()) == 81  # the first move can go anywhere
    board = replay_board(True, [4 * 9 + 2], [])  # x plays cell 2 of the middle board...
    assert board.forced == 2 and board.legal() == list(range(18, 27))  # ...so o must play in board 2
    assert not board.play(0)  # board 0 isn't allowed
    # Three in a row in a small board wins it.
    board = play.Board(True, [""] * 81)
    board.cells[0] = board.cells[1] = "x"
    board.forced = 0
    assert board.play(2) and board.small[0] == "x" and board.forced == 2
    # Sent to a decided board, you may play in any open board.
    board = play.Board(True, [""] * 81)
    board.small[3] = "o"
    assert board.play(4 * 9 + 3)  # cell 3 of the middle board would send o to board 3...
    assert board.forced is None and all(m // 9 != 3 for m in board.legal())  # ...which is decided


def test_ultimate_is_won_with_three_small_boards_in_a_row():
    board = play.Board(True, [""] * 81)
    board.small = ["x", "x", "", "", "", "", "", "", ""]
    for c in (0, 1):  # x plays board 2 cells 0 and 1, o answers in board 2 from elsewhere
        board.cells[18 + c] = "x"
    board.forced = 2
    board.turn = 0
    assert board.play(18 + 2)
    assert board.small[2] == "x" and board.winner == 0 and board.line == (0, 1, 2)


def test_rock_paper_scissors_best_of_three():
    gid = "1" * 32
    picks = [("rock", "scissors"), ("paper", "paper"), ("scissors", "rock"), ("paper", "rock")]
    commits, reveals = [[], []], [[], []]
    for r, pair in enumerate(picks):
        for seat in (0, 1):
            salt = f"{seat}{r}".ljust(32, "0")
            commits[seat].append(seal(gid, r, pair[seat], salt))
            reveals[seat].append([pair[seat], salt])
    result = replay_rounds(gid, 3, commits, reveals)
    assert [w for _, _, w in result.rounds] == [0, None, 1, 0]
    assert result.score == [2, 1] and result.winner == 0
    # Locked in but not revealed: nothing is decided.
    partial = replay_rounds(gid, 3, [commits[0][:1], commits[1][:1]], [reveals[0][:1], []])
    assert partial.rounds == [] and partial.winner is None


def test_a_changed_choice_is_caught():
    gid = "1" * 32
    commits = [[seal(gid, 0, "rock", "0" * 32)], [seal(gid, 0, "paper", "1" * 32)]]
    reveals = [[["rock", "0" * 32]], [["scissors", "1" * 32]]]  # seat 1 locked in paper, then said scissors
    result = replay_rounds(gid, 3, commits, reveals)
    assert result.cheat == 1 and result.winner == 0


def test_records_from_other_peers_are_validated():
    good = {"id": "1" * 32, "kind": "tictactoe", "players": [A_ID, B_ID], "names": ["A", "B"], "host": A_ID,
            "best_of": 1, "stance": "offered", "moves": [4]}
    assert parse_record(good).moves == [4]
    for bad in [
        dict(good, kind="chess"), dict(good, players=[A_ID, A_ID]), dict(good, host=C_ID),
        dict(good, moves=[9]), dict(good, moves=[True]), dict(good, stance="winning"), dict(good, id="x"),
        dict(good, kind="rps", best_of=4), dict(good, kind="rps", best_of=3, commits=["zz"]),
        dict(good, kind="rps", best_of=3, commits=[], reveals=[["lizard", "0" * 32]]),
    ]:
        assert parse_record(bad) is None, bad
    assert parse_record(dict(good, names=["A‮", 5]))  # bad names are replaced, not fatal


# ----- two players, without a network ------------------------------------------------------------------

def arcade(me, name, others, inviter_first=True):
    peers = {pid: SimpleNamespace(peer_id=pid, name=pname, online=True, play=True, games=frozenset(play.TITLES),
                                  base_url=lambda: "")
             for pid, pname in others.items()}
    table = SimpleNamespace(get=peers.get, online=lambda: [p for p in peers.values() if p.online])
    a = Arcade(me, lambda: name, Authenticator(None, me), table, 3.0)
    a.peer_table = peers
    if inviter_first:  # most tests want the one who invites to move first
        invite = a.invite
        a.invite = lambda peer_id, kind, best_of=3, first=True: invite(peer_id, kind, best_of, first)
    return a


@pytest.fixture
def pair():
    return arcade(A_ID, "Alice", {B_ID: "Bob"}), arcade(B_ID, "Bob", {A_ID: "Alice"})


def sync(*arcades):
    """Each reads what the others publish, twice (an answer can cause another answer)."""
    for _ in range(2):
        for x in arcades:
            for y in arcades:
                if x is not y:
                    y.merge(x.me, json.loads(x.published()[0]))


def only_game(a):
    games = a.view()["games"]
    assert len(games) == 1
    return games[0]


def test_invite_accept_and_play_to_a_win(pair):
    a, b = pair
    game = a.invite(B_ID, "tictactoe")
    assert game["status"] == "waiting" and game["mark"] == "x"
    sync(a, b)
    invites = b.view()["invites"]
    assert [(i["title"], i["from"]["name"], i["you_go_first"]) for i in invites] == [("Tic-tac-toe", "Alice", False)]
    b.answer(game["id"], True)
    sync(a, b)
    assert only_game(a)["status"] == "playing" and only_game(a)["your_turn"]
    assert not only_game(b)["your_turn"]
    with pytest.raises(PlayError):
        b.move(game["id"], 4)  # not Bob's turn
    for who, cell in [(a, 0), (b, 4), (a, 1), (b, 5), (a, 2)]:
        who.move(game["id"], cell)
        sync(a, b)
    ga, gb = only_game(a), only_game(b)
    assert (ga["status"], ga["result"], gb["result"]) == ("over", "won", "lost")
    assert ga["board"] == gb["board"] and ga["line"] == [0, 1, 2]


def test_ultimate_moves_follow_the_rules_on_both_sides(pair):
    a, b = pair
    gid = a.invite(B_ID, "ultimate")["id"]
    sync(a, b)
    b.answer(gid, True)
    sync(a, b)
    a.move(gid, 40)  # centre of the centre board: Bob must play in the centre board
    sync(a, b)
    gb = only_game(b)
    assert gb["forced"] == 4 and gb["allowed"] == [36, 37, 38, 39, 41, 42, 43, 44]
    with pytest.raises(PlayError):
        b.move(gid, 0)
    b.move(gid, 36)
    sync(a, b)
    assert only_game(a)["forced"] == 0 and only_game(a)["board"] == only_game(b)["board"]


def test_declining_and_withdrawing(pair):
    a, b = pair
    first = a.invite(B_ID, "tictactoe")["id"]
    second = a.invite(B_ID, "rps", best_of=5)["id"]
    sync(a, b)
    assert len(b.view()["invites"]) == 2
    b.answer(first, False)
    a.withdraw(second)
    sync(a, b)
    assert b.view()["invites"] == [] and b.view()["games"] == []
    declined = only_game(a)
    assert declined["status"] == "declined" and "Bob" in declined["reason"]
    a.close(first)
    assert a.view()["games"] == []


def test_resigning_and_rematch_swap_who_starts(pair):
    a, b = pair
    gid = a.invite(B_ID, "tictactoe")["id"]
    sync(a, b)
    b.answer(gid, True)
    sync(a, b)
    b.resign(gid)
    sync(a, b)
    assert (only_game(a)["result"], only_game(a)["reason"]) == ("won", "Bob resigned")
    again = a.rematch(gid)
    assert again["mark"] == "o"  # Bob goes first this time
    sync(a, b)
    assert b.view()["invites"][0]["you_go_first"] is True


def test_rock_paper_scissors_locks_in_before_anyone_sees(pair):
    a, b = pair
    gid = a.invite(B_ID, "rps", best_of=1)["id"]
    sync(a, b)
    b.answer(gid, True)
    sync(a, b)
    a.choose(gid, "rock")
    sync(a, b)
    published = json.loads(a.published()[0])["games"][0]
    assert published["reveals"] == [] and len(published["commits"]) == 1  # sealed until Bob locks in too
    gb = only_game(b)
    assert gb["rps"]["they_locked"] and gb["your_turn"] and gb["rps"]["rounds"] == []
    with pytest.raises(PlayError):
        a.choose(gid, "paper")  # already locked in
    b.choose(gid, "paper")
    sync(a, b)
    ga, gb = only_game(a), only_game(b)
    assert ga["rps"]["rounds"] == [{"mine": "rock", "theirs": "paper", "result": "lost"}]
    assert (ga["result"], gb["result"]) == ("lost", "won")


def test_back_to_back_games_take_turns_going_first():
    a = arcade(A_ID, "Alice", {B_ID: "Bob"}, inviter_first=False)
    b = arcade(B_ID, "Bob", {A_ID: "Alice"}, inviter_first=False)
    gid = a.invite(B_ID, "tictactoe", first=True)["id"]
    sync(a, b)
    b.answer(gid, True)
    sync(a, b)
    # The next game, of any kind and from either side (not just Rematch): the other one starts.
    nxt = a.invite(B_ID, "connect4")
    assert nxt["seat"] == 1
    sync(a, b)
    assert b.view()["invites"][0]["you_go_first"] is True
    b.answer(nxt["id"], True)
    sync(a, b)
    assert b.invite(A_ID, "dots")["seat"] == 1  # Bob started the last one: Alice's turn to start
    # An invitation nobody accepted isn't a game played: it doesn't count.
    assert a.invite(B_ID, "memory")["seat"] == 0
    assert a.invite(B_ID, "checkers")["seat"] == 0


def test_who_starts_a_first_game_is_random(monkeypatch):
    for roll, seat in ((0.2, 0), (0.7, 1)):
        monkeypatch.setattr(play, "_random", SimpleNamespace(random=lambda roll=roll: roll))
        a = arcade(A_ID, "Alice", {B_ID: "Bob"}, inviter_first=False)
        assert a.invite(B_ID, "tictactoe")["seat"] == seat


def test_a_peer_can_only_play_its_own_side(pair):
    a, b = pair
    gid = a.invite(B_ID, "tictactoe")["id"]
    sync(a, b)
    b.answer(gid, True)
    sync(a, b)
    record = json.loads(b.published()[0])
    record["games"][0]["moves"] = [0, 1, 2]  # Bob claims three moves before Alice made any
    a.merge(B_ID, record)
    assert only_game(a)["board"] == [""] * 9 and only_game(a)["your_turn"]
    # A third peer can't add anything to their game.
    forged = dict(record["games"][0], moves=[4])
    a.merge(C_ID, {"peer_id": C_ID, "games": [forged]})
    assert only_game(a)["board"] == [""] * 9


def test_invitations_from_peers_that_leave_disappear(pair):
    a, b = pair
    a.invite(B_ID, "tictactoe")
    sync(a, b)
    assert b.view()["invites"]
    b.peer_table[A_ID].online = False
    b._housekeeping()
    assert b.view()["invites"] == []


def test_inviting_needs_an_online_player_with_games(pair):
    a, _ = pair
    with pytest.raises(PlayError):
        a.invite(C_ID, "tictactoe")
    a.peer_table[B_ID].play = False
    with pytest.raises(PlayError, match="older LightningLAN"):
        a.invite(B_ID, "tictactoe")
    with pytest.raises(PlayError):
        a.invite(B_ID, "chess")


# ----- the HTTP routes ----------------------------------------------------------------------------------

def test_games_are_published_to_the_lan_and_played_locally(peer):
    r = peer.get("/api/games", client=LAN_CLIENT)
    assert r.status_code == 200 and r.json() == {"peer_id": peer.settings.peer_id, "games": []}
    assert peer.get("/api/games", client=LAN_CLIENT, headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    assert peer.post("/api/games", client=LAN_CLIENT).status_code == 405
    view = peer.get("/api/play").json()
    assert view["games"] == [] and view["titles"]["ultimate"] == "Ultimate tic-tac-toe"
    assert peer.get("/api/play", client=LAN_CLIENT).status_code == 403
    r = peer.post("/api/play/invite", json={"peer_id": B_ID, "kind": "tictactoe"}, headers=UI)
    assert r.status_code == 404 and "online" in r.json()["detail"]
    assert peer.post("/api/play/" + "1" * 32 + "/resign", headers=UI).status_code == 404
    assert peer.post("/api/play/" + "1" * 32 + "/fly", headers=UI).status_code == 404


def test_the_files_index_says_this_peer_plays(peer):
    assert peer.get("/api/files", client=LAN_CLIENT).json()["play"] == 1


def test_the_phone_page_plays_as_this_computer(peer):
    from .test_phone import paired

    assert peer.get("/phone/api/play", client=LAN_CLIENT).status_code == 401
    phone = paired(peer)
    r = peer.get("/phone/api/play", client=LAN_CLIENT, headers=phone)
    assert r.status_code == 200 and r.json()["games"] == []
    r = peer.post("/phone/api/play/invite", client=LAN_CLIENT, headers=phone, json={"peer_id": B_ID, "kind": "rps"})
    assert r.status_code == 403  # no X-LightningLAN-Client header
    r = peer.post("/phone/api/play/invite", client=LAN_CLIENT, headers={**phone, **UI},
                  json={"peer_id": B_ID, "kind": "rps"})
    assert r.status_code == 404  # allowed, but that player isn't online


# ----- with real peer processes -------------------------------------------------------------------------

def game_of(peer, game_id):
    return next(g for g in peer.get("/api/play").json()["games"] if g["id"] == game_id)


@pytest.mark.integration
def test_two_computers_play_tictactoe_and_rock_paper_scissors(spawn):
    from .harness import start_together

    a, b = spawn("A", start=False), spawn("B", start=False)
    start_together(a, b)
    wait_for(lambda: any(o["peer_id"] == b.peer_id for o in a.get("/api/play").json()["opponents"]),
             timeout=30, message="A to see B as an opponent")
    gid = a.post("/api/play/invite", json={"peer_id": b.peer_id, "kind": "tictactoe"}).json()["id"]
    wait_for(lambda: b.get("/api/play").json()["invites"], timeout=15, message="B to get the invitation")
    assert b.post(f"/api/play/{gid}/accept").status_code == 200
    wait_for(lambda: game_of(a, gid)["status"] == "playing", timeout=15, message="A to see the game start")
    x, o = (a, b) if game_of(a, gid)["mark"] == "x" else (b, a)  # who starts is random
    for player, cell in [(x, 0), (o, 4), (x, 1), (o, 8), (x, 2)]:
        wait_for(lambda: game_of(player, gid)["your_turn"], timeout=15, message="a turn")
        r = player.post(f"/api/play/{gid}/move", json={"cell": cell})
        assert r.status_code == 200, r.text
    wait_for(lambda: game_of(o, gid)["status"] == "over", timeout=15, message="O to see the end")
    assert (game_of(x, gid)["result"], game_of(o, gid)["result"]) == ("won", "lost")

    rps = b.post("/api/play/invite", json={"peer_id": a.peer_id, "kind": "rps", "best_of": 1}).json()["id"]
    wait_for(lambda: a.get("/api/play").json()["invites"], timeout=15, message="A to get the invitation")
    a.post(f"/api/play/{rps}/accept")
    wait_for(lambda: game_of(b, rps)["your_turn"], timeout=15, message="B to see the match start")
    b.post(f"/api/play/{rps}/move", json={"choice": "scissors"})
    a.post(f"/api/play/{rps}/move", json={"choice": "rock"})
    wait_for(lambda: game_of(b, rps)["status"] == "over" and game_of(a, rps)["status"] == "over",
             timeout=15, message="both to see the result")
    assert game_of(a, rps)["result"] == "won"
    assert game_of(b, rps)["rps"]["rounds"] == [{"mine": "scissors", "theirs": "rock", "result": "lost"}]


# ----- the other games, between two players without a network -------------------------------------

def started(a, b, kind, **kwargs):
    gid = a.invite(B_ID, kind, **kwargs)["id"]
    sync(a, b)
    b.answer(gid, True)
    sync(a, b)
    return gid


def test_battleship_hidden_fleets_answers_and_the_final_check(pair):
    a, b = pair
    gid = started(a, b, "battleship")
    assert only_game(a)["battle"]["phase"] == "setup"
    a.shuffle_fleet(gid)
    assert len(only_game(a)["battle"]["ships"]) == 5  # my ships, only on my side
    a.fleet_ready(gid)
    b.fleet_ready(gid)
    sync(a, b)
    published = json.loads(a.published()[0])["games"][0]
    assert published["reveal"] is None and len(published["fleet"]) == 64  # sealed, not shown
    targets = [cell for ship in b._games[gid].fleet and play.ship_cells(b._games[gid].fleet) for cell in ship]
    misses = [c for c in range(100) if c not in {x for s in play.ship_cells(a._games[gid].fleet) for x in s}]
    for k, target in enumerate(targets):
        assert only_game(a)["your_turn"]
        a.fire(gid, target)
        sync(a, b)
        if only_game(a)["status"] == "over":
            break
        b.fire(gid, misses[k])
        sync(a, b)
    ga, gb = only_game(a), only_game(b)
    assert (ga["result"], gb["result"]) == ("won", "lost")
    assert ga["battle"]["sunk"] == [5, 0] and all(s["result"] in ("hit", "sunk") for s in ga["battle"]["outgoing"])
    sync(a, b)  # both fleets are revealed once it's over
    assert len(only_game(b)["battle"]["their_ships"]) == 5 and only_game(a)["result"] == "won"


def test_battleship_a_fleet_that_lied_loses(pair):
    a, b = pair
    gid = started(a, b, "battleship")
    a.fleet_ready(gid)
    b.fleet_ready(gid)
    sync(a, b)
    b_cells = [c for s in play.ship_cells(b._games[gid].fleet) for c in s]
    a.fire(gid, b_cells[0])
    sync(a, b)
    record = json.loads(b.published()[0])
    record["games"][0]["answers"] = ["miss"]  # Bob's side claims a miss
    record["games"][0]["reveal"] = [play.layout_text(b._games[gid].fleet), b._games[gid].fleet_salt]
    a.merge(B_ID, record)
    game = only_game(a)
    assert game["result"] == "won" and "didn't match" in game["reason"]


def test_typing_race_quicker_wins_and_times_stay_hidden(pair):
    a, b = pair
    gid = started(a, b, "typing")
    sentence = only_game(a)["race"]["sentence"]
    assert sentence == only_game(b)["race"]["sentence"]
    b.finish(gid, 9000)
    sync(a, b)
    race = only_game(a)["race"]
    assert race["they_done"] and race["their_time"] is None  # not until you're done too
    with pytest.raises(PlayError):
        a.finish(gid, 5)  # not believable
    a.finish(gid, 8000)
    sync(a, b)
    assert only_game(a)["result"] == "won" and only_game(a)["race"]["their_time"] == 9000


def test_math_sprint_scores_each_sum(pair):
    a, b = pair
    gid = started(a, b, "math")
    problems = only_game(a)["sprint"]["problems"]
    assert len(problems) == 10 and problems == only_game(b)["sprint"]["problems"]
    for k in range(10):
        a.finish(gid, 1000 + (500 if k < 3 else 0))
        b.finish(gid, 1200)
    sync(a, b)
    ga = only_game(a)
    assert ga["sprint"]["score"] == [7, 3] and ga["result"] == "won"


def test_memory_match_both_see_the_same_cards(pair):
    a, b = pair
    gid = started(a, b, "memory")
    layout = play.memory_layout(gid)
    first_pair = [i for i, v in enumerate(layout) if v == layout[0]]
    a.move(gid, first_pair[0])
    sync(a, b)
    assert only_game(b)["cards"][first_pair[0]] == layout[0]  # the turned card shows on both sides
    assert only_game(b)["cards"].count(None) == 15
    a.move(gid, first_pair[1])
    sync(a, b)
    assert only_game(a)["score"] == [1, 0] and only_game(a)["your_turn"]
    miss = next(i for i in range(16) if layout[i] != layout[1] and i not in first_pair and i != 1)
    a.move(gid, 1)
    a.move(gid, miss)
    sync(a, b)
    assert only_game(b)["your_turn"] and only_game(b)["shown"] == [1, miss]


def game(arcade, gid):
    return next(g for g in arcade.view()["games"] if g["id"] == gid)


def test_four_in_a_row_checkers_and_dots_take_turns(pair):
    a, b = pair
    gid = started(a, b, "connect4")
    for player, column in [(a, 3), (b, 4), (a, 3), (b, 4), (a, 3), (b, 4), (a, 3)]:
        player.move(gid, column)
        sync(a, b)
    assert game(a, gid)["result"] == "won" and game(b, gid)["line"] == [17, 24, 31, 38]
    gid = started(a, b, "checkers")
    move = game(a, gid)["allowed"][0]
    a.move(gid, move)
    sync(a, b)
    assert game(b, gid)["your_turn"] and game(b, gid)["board"][move[-1]] == "x"
    with pytest.raises(PlayError):
        b.move(gid, [move[-1], move[0]])  # not Bob's piece
    gid = started(a, b, "dots")
    for line in (0, 5, 30):
        (a if game(a, gid)["your_turn"] else b).move(gid, line)
        sync(a, b)
    closer = a if game(a, gid)["your_turn"] else b
    closer.move(gid, 31)  # closes the first box: the same player goes again
    sync(a, b)
    assert game(closer, gid)["your_turn"] and game(closer, gid)["score"] == [1, 0]


def test_older_peers_are_only_offered_the_games_they_have(pair):
    a, _ = pair
    a.peer_table[B_ID].games = None  # a LightningLAN from before the games list
    assert a.invite(B_ID, "tictactoe")["status"] == "waiting"
    with pytest.raises(PlayError, match="doesn't have"):
        a.invite(B_ID, "battleship")
