"""While you wait: head-to-head games between peers.

Every peer publishes only what it did itself, at GET /api/games: the games it offers, its answer
to invitations, its own moves, and what each game needs besides (sealed rock-paper-scissors
choices, a sealed battleship fleet and its answers to shots, race times). Each side polls the
other and replays both halves with the same rules (games.py), so both see the same game. Nothing
is ever sent to another peer, so the peer-to-peer API stays read-only, and a peer can only ever
play its own side.

The Android app (android/core/.../Play.kt and Games.kt) implements the same rules and records.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from random import SystemRandom
from typing import Callable

import httpx

from .auth import Authenticator
from .games import (
    CHOICES, FLEET, MATH_PROBLEMS, MAX_ROUNDS, SHIP_NAMES, Board, Checkers, Dots, FourInARow, Memory, Race,
    Rounds, answer_shots, interleave, layout_text, math_problems, memory_layout, parse_layout, random_layout,
    replay_battle, replay_board, replay_race, replay_rounds, replay_turns, seal, seal_fleet, sentence_for,
    ship_cells,
)
from .pathsafe import display_name
from .peers import PeerTable

__all__ = ["Arcade", "PlayError", "Record", "parse_record", "interleave", "replay_board", "replay_rounds", "seal"]

log = logging.getLogger(__name__)

TICTACTOE, ULTIMATE, CONNECT4, CHECKERS, DOTS = "tictactoe", "ultimate", "connect4", "checkers", "dots"
BATTLESHIP, MEMORY, RPS, TYPING, MATH = "battleship", "memory", "rps", "typing", "math"
TITLES = {
    TICTACTOE: "Tic-tac-toe",
    ULTIMATE: "Ultimate tic-tac-toe",
    CONNECT4: "Four in a row",
    CHECKERS: "Checkers",
    DOTS: "Dots and boxes",
    BATTLESHIP: "Battleship",
    MEMORY: "Memory match",
    RPS: "Rock-paper-scissors",
    TYPING: "Typing race",
    MATH: "Math sprint",
}
TURN_KINDS = {TICTACTOE, ULTIMATE, CONNECT4, CHECKERS, DOTS, MEMORY}  # one move after another
# The most moves one player can make in each, and how many different moves there are.
MOVE_LIMITS = {TICTACTOE: (5, 9), ULTIMATE: (41, 81), CONNECT4: (21, 7), DOTS: (60, 60), MEMORY: (400, 16)}
CHECKERS_LIMIT = 600
BEST_OF = (1, 3, 5, 7)
MARKS = ("x", "o")

OFFERED, ACCEPTED, DECLINED, CANCELED, RESIGNED = "offered", "accepted", "declined", "canceled", "resigned"
HOST_STANCES = {OFFERED, CANCELED, RESIGNED}
GUEST_STANCES = {ACCEPTED, DECLINED, RESIGNED}
# What a game is, from one player's side.
WAITING, PLAYING, OVER = "waiting", "playing", "over"  # also DECLINED and CANCELED

INVITE_TTL = 600.0  # an invitation nobody answers is withdrawn after this many seconds
KEEP_CLOSED = 900.0  # a game closed here stays published this long, so the opponent sees how it ended
MAX_GAMES = 40
ACTIVE_POLL = 0.5  # seconds between looks at an opponent in a game (others: the peer refresh interval)
MAX_PAYLOAD = 2 * 1024 * 1024
TYPING_TIMES, MATH_TIMES = (300, 600_000), (100, 600_000)  # believable times, in milliseconds
ANSWERS = ("miss", "hit", "sunk")

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_random = SystemRandom()


class PlayError(Exception):
    """A game action was refused (message is user-facing)."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


# ----- what each peer publishes ----------------------------------------------------------------------

@dataclass
class Record:
    """One peer's half of a game, as it publishes it."""

    id: str
    kind: str
    players: tuple[str, str]  # seat 0 moves first
    names: tuple[str, str]
    host: str
    best_of: int
    stance: str
    moves: list = field(default_factory=list)  # turn games: this peer's moves (checkers: paths)
    commits: list[str] = field(default_factory=list)  # rock-paper-scissors: sealed choices
    reveals: list[list[str]] = field(default_factory=list)  # [choice, salt] once both have locked in
    fleet: str = ""  # battleship: the sealed fleet ("" while placing ships)
    shots: list[int] = field(default_factory=list)  # battleship: squares fired at
    answers: list[str] = field(default_factory=list)  # battleship: this fleet's answers to the other's shots
    reveal: list[str] | None = None  # battleship: [layout, salt], once the battle is over
    times: list[int] = field(default_factory=list)  # typing race (one) or math sprint (one per sum), in ms

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "kind": self.kind,
            "players": list(self.players),
            "names": list(self.names),
            "host": self.host,
            "best_of": self.best_of,
            "stance": self.stance,
        }
        if self.kind == RPS:
            d["commits"] = list(self.commits)
            d["reveals"] = [list(r) for r in self.reveals]
        elif self.kind == BATTLESHIP:
            d["fleet"] = self.fleet
            d["shots"] = list(self.shots)
            d["answers"] = list(self.answers)
            d["reveal"] = list(self.reveal) if self.reveal else None
        elif self.kind in (TYPING, MATH):
            d["times"] = list(self.times)
        else:
            d["moves"] = [list(m) if isinstance(m, (list, tuple)) else m for m in self.moves]
        return d

    def same_game(self, other: Record) -> bool:
        return (self.id, self.kind, self.players, self.host, self.best_of) == (
            other.id, other.kind, other.players, other.host, other.best_of)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int_list(value: object, limit: int, below: int, low: int = 0) -> list[int] | None:
    if not isinstance(value, list) or len(value) > limit:
        return None
    if not all(_is_int(v) and low <= v < below for v in value):
        return None
    return list(value)


def parse_record(data: object) -> Record | None:
    """Validate another peer's record of a game. None if anything about it is off."""
    if not isinstance(data, dict):
        return None
    game_id, kind, host, stance = data.get("id"), data.get("kind"), data.get("host"), data.get("stance")
    players, names = data.get("players"), data.get("names")
    if not (isinstance(game_id, str) and _ID_RE.match(game_id)) or kind not in TITLES:
        return None
    if not (isinstance(players, list) and len(players) == 2 and all(isinstance(p, str) and _ID_RE.match(p) for p in players)):
        return None
    if players[0] == players[1] or host not in players or stance not in HOST_STANCES | GUEST_STANCES:
        return None
    if not (isinstance(names, list) and len(names) == 2 and all(isinstance(n, str) for n in names)):
        names = [p[:8] for p in players]
    names = tuple(display_name(n).strip()[:64] or p[:8] for n, p in zip(names, players))
    best_of = data.get("best_of", 1)
    if kind == RPS:
        if best_of not in BEST_OF:
            return None
    else:
        best_of = 1
    record = Record(game_id, kind, (players[0], players[1]), names, host, best_of, stance)
    if kind == RPS:
        commits, reveals = data.get("commits", []), data.get("reveals", [])
        if not (isinstance(commits, list) and len(commits) <= MAX_ROUNDS
                and all(isinstance(c, str) and _HASH_RE.match(c) for c in commits)):
            return None
        if not (isinstance(reveals, list) and len(reveals) <= MAX_ROUNDS):
            return None
        for r in reveals:
            if not (isinstance(r, list) and len(r) == 2 and r[0] in CHOICES and isinstance(r[1], str) and _ID_RE.match(r[1])):
                return None
        record.commits, record.reveals = list(commits), [list(r) for r in reveals]
    elif kind == BATTLESHIP:
        fleet, reveal = data.get("fleet", ""), data.get("reveal")
        if not (fleet == "" or (isinstance(fleet, str) and _HASH_RE.match(fleet))):
            return None
        shots = _int_list(data.get("shots", []), 100, 100)
        answers = data.get("answers", [])
        if shots is None or not (isinstance(answers, list) and len(answers) <= 100 and all(a in ANSWERS for a in answers)):
            return None
        if reveal is not None:
            if not (isinstance(reveal, list) and len(reveal) == 2 and all(isinstance(x, str) for x in reveal)
                    and parse_layout(reveal[0]) is not None and _ID_RE.match(reveal[1])):
                return None
        record.fleet, record.shots, record.answers = fleet, shots, list(answers)
        record.reveal = list(reveal) if reveal else None
    elif kind in (TYPING, MATH):
        low, high = TYPING_TIMES if kind == TYPING else MATH_TIMES
        times = _int_list(data.get("times", []), 1 if kind == TYPING else MATH_PROBLEMS, high + 1, low)
        if times is None:
            return None
        record.times = times
    elif kind == CHECKERS:
        moves = data.get("moves", [])
        if not (isinstance(moves, list) and len(moves) <= CHECKERS_LIMIT):
            return None
        for path in moves:
            if _int_list(path, 13, 64) is None or len(path) < 2:
                return None
        record.moves = [list(p) for p in moves]
    else:
        limit, below = MOVE_LIMITS[kind]
        moves = _int_list(data.get("moves", []), limit, below)
        if moves is None:
            return None
        record.moves = moves
    return record


# ----- this peer's games -------------------------------------------------------------------------------

@dataclass
class Game:
    mine: Record
    theirs: Record | None = None  # the opponent's half, as last seen (kept if they stop publishing it)
    secrets: dict[int, list[str]] = field(default_factory=dict)  # rock-paper-scissors: [choice, salt] until revealed
    choices: dict[int, str] = field(default_factory=dict)  # rock-paper-scissors: what I picked each round
    fleet: list[tuple[int, bool]] | None = None  # battleship: my ships (never published until the end)
    fleet_salt: str = ""
    created: float = field(default_factory=time.time)
    closed_at: float | None = None  # closed here: no longer shown, still published for a while

    @property
    def id(self) -> str:
        return self.mine.id

    def seat_of(self, peer_id: str) -> int:
        return self.mine.players.index(peer_id)


@dataclass
class Outcome:
    status: str
    result: str | None = None  # "won", "lost" or "draw" when over
    reason: str = ""
    engine: object = None  # the replayed game (a Board, FourInARow, Checkers, Dots or Memory)
    rounds: Rounds | None = None
    battle: object = None
    race: Race | None = None


def new_engine(kind: str, game_id: str):
    if kind in (TICTACTOE, ULTIMATE):
        return Board(kind == ULTIMATE, [""] * (81 if kind == ULTIMATE else 9))
    if kind == CONNECT4:
        return FourInARow()
    if kind == CHECKERS:
        return Checkers()
    if kind == DOTS:
        return Dots()
    return Memory(memory_layout(game_id))


def _fleet_ok(game_id: str, record: Record, shots_at_it: list[int]) -> bool:
    """Whether a revealed fleet is the one that was sealed, and told the truth about every shot."""
    if not record.reveal or not record.fleet:
        return False
    text, salt = record.reveal
    layout = parse_layout(text)
    if layout is None or seal_fleet(game_id, text, salt) != record.fleet:
        return False
    truth = answer_shots(ship_cells(layout), shots_at_it[:len(record.answers)])
    return truth == record.answers[:len(truth)]


class Arcade:
    """This peer's games: what the local web UI and phone page show and do, and the polling of opponents."""

    def __init__(self, own_peer_id: str, own_name: Callable[[], str], auth: Authenticator, peers: PeerTable,
                 refresh_interval: float):
        self.me = own_peer_id
        self.own_name = own_name
        self.auth = auth
        self.peers = peers
        self.refresh_interval = refresh_interval
        self.version = 0  # bumps on every change
        self._games: dict[str, Game] = {}
        self._invites: dict[str, Record] = {}  # offered to us, not answered yet
        self._lock = threading.RLock()
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        self._polled: dict[str, float] = {}
        self._etags: dict[str, str] = {}
        self._inflight: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._published: tuple[int, bytes, str] | None = None

    # ----- lifecycle --------------------------------------------------------------------------

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0), trust_env=False)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for t in list(self._tasks):
            t.cancel()
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
        self._polled.clear()
        self._etags.clear()
        self._inflight.clear()

    def _changed(self) -> None:
        self.version += 1

    # ----- what other peers read (GET /api/games) -------------------------------------------------

    def published(self) -> tuple[bytes, str]:
        with self._lock:
            cached = self._published
            if cached is not None and cached[0] == self.version:
                return cached[1], cached[2]
            body = json.dumps(
                {"peer_id": self.me, "games": [g.mine.to_dict() for g in self._games.values()]},
                separators=(",", ":"),
            ).encode("utf-8")
            etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
            self._published = (self.version, body, etag)
            return body, etag

    # ----- the outcome of a game, seen from here --------------------------------------------------

    def _halves(self, g: Game) -> tuple[Record | None, Record | None]:
        """Both players' records, by seat."""
        halves: list[Record | None] = [None, None]
        halves[g.seat_of(self.me)] = g.mine
        halves[1 - g.seat_of(self.me)] = g.theirs
        return halves[0], halves[1]

    def outcome(self, g: Game) -> Outcome:
        mine, theirs = g.mine, g.theirs
        host_is_me = mine.host == self.me
        their_stance = theirs.stance if theirs else None
        host_stance = mine.stance if host_is_me else (their_stance or OFFERED)
        guest_stance = their_stance if host_is_me else mine.stance
        opponent = self._opponent_name(g)
        if guest_stance not in (ACCEPTED, RESIGNED, DECLINED):
            if host_stance == CANCELED:
                return Outcome(CANCELED, reason="Invitation withdrawn")
            return Outcome(WAITING)
        if guest_stance == DECLINED:
            return Outcome(DECLINED, reason=f"{opponent} said no thanks" if host_is_me else "You declined")
        my_seat = g.seat_of(self.me)
        # The host withdrawing just as the guest accepted counts as resigning, on both sides.
        i_gave_up = mine.stance == RESIGNED or (host_is_me and mine.stance == CANCELED)
        they_gave_up = their_stance == RESIGNED or (not host_is_me and their_stance == CANCELED)
        if i_gave_up and they_gave_up:
            return Outcome(OVER, "draw", "You both gave up")
        if i_gave_up:
            return Outcome(OVER, "lost", "You resigned")
        if they_gave_up:
            return Outcome(OVER, "won", f"{opponent} resigned")
        first, second = self._halves(g)

        def finished(winner: int | None, draw: bool, why: str, **parts) -> Outcome:
            if draw:
                return Outcome(OVER, "draw", why, **parts)
            if winner is None:
                return Outcome(PLAYING, **parts)
            return Outcome(OVER, "won" if winner == my_seat else "lost", why, **parts)

        kind = mine.kind
        if kind == RPS:
            commits = [r.commits if r else [] for r in (first, second)]
            reveals = [r.reveals if r else [] for r in (first, second)]
            rounds = replay_rounds(mine.id, mine.best_of, commits, reveals)
            if rounds.cheat is not None:
                if rounds.cheat == my_seat:
                    return Outcome(OVER, "lost", "Your choice didn't match what you locked in", rounds=rounds)
                return Outcome(OVER, "won", f"{opponent}'s choice didn't match what they locked in", rounds=rounds)
            return finished(rounds.winner, rounds.draw, f"{MAX_ROUNDS} rounds without a winner" if rounds.draw else "",
                            rounds=rounds)
        if kind == BATTLESHIP:
            shots = [r.shots if r else [] for r in (first, second)]
            answers = [r.answers if r else [] for r in (first, second)]
            battle = replay_battle(shots, answers) if (first and first.fleet and second and second.fleet) else None
            if theirs is not None and theirs.reveal and not _fleet_ok(mine.id, theirs, mine.shots):
                return Outcome(OVER, "won", f"{opponent}'s fleet didn't match their answers", battle=battle)
            if battle is None:
                return Outcome(PLAYING)
            return finished(battle.winner, False, "Fleet sunk", battle=battle)
        if kind in (TYPING, MATH):
            race = replay_race([r.times if r else [] for r in (first, second)], 1 if kind == TYPING else MATH_PROBLEMS)
            why = "Quicker" if kind == TYPING else f"{race.points[my_seat]} sums to {race.points[1 - my_seat]}"
            return finished(race.winner, race.draw, why, race=race)
        engine = replay_turns(new_engine(kind, mine.id), first.moves if first else [], second.moves if second else [])
        if kind == DOTS or kind == MEMORY:
            unit = "boxes" if kind == DOTS else "pairs"
            why = f"{engine.score[my_seat]} {unit} to {engine.score[1 - my_seat]}"
        elif kind == CHECKERS:
            why = "40 moves each without a capture" if engine.draw else "No moves left"
        elif kind == CONNECT4:
            why = "Four in a row" if engine.winner is not None else "The board is full"
        else:
            why = "Three in a row" if engine.winner is not None else "No more moves"
        return finished(engine.winner, engine.draw, why, engine=engine)

    def _opponent_id(self, g: Game) -> str:
        a, b = g.mine.players
        return b if a == self.me else a

    def _opponent_name(self, g: Game) -> str:
        pid = self._opponent_id(g)
        peer = self.peers.get(pid)
        if peer is not None and peer.name:
            return peer.name
        return g.mine.names[g.seat_of(pid)]

    # ----- what the web UI shows (GET /api/play) ----------------------------------------------------

    def view(self) -> dict:
        with self._lock:
            games = [self._game_view(g) for g in sorted(self._games.values(), key=lambda g: -g.created)
                     if g.closed_at is None]
            invites = [self._invite_view(r) for r in self._invites.values()]
        opponents = [{"peer_id": p.peer_id, "name": p.name} for p in self.peers.online() if p.play]
        return {"games": games, "invites": invites, "opponents": opponents, "titles": TITLES, "best_of": list(BEST_OF)}

    def _invite_view(self, r: Record) -> dict:
        host_seat = r.players.index(r.host)
        peer = self.peers.get(r.host)
        return {
            "id": r.id,
            "kind": r.kind,
            "title": TITLES[r.kind],
            "best_of": r.best_of,
            "from": {"peer_id": r.host, "name": peer.name if peer else r.names[host_seat]},
            "you_go_first": r.players[0] == self.me,
        }

    def _game_view(self, g: Game) -> dict:
        out = self.outcome(g)
        my_seat = g.seat_of(self.me)
        them = 1 - my_seat
        opponent_id = self._opponent_id(g)
        peer = self.peers.get(opponent_id)
        playing = out.status == PLAYING
        kind = g.mine.kind
        view = {
            "id": g.id,
            "kind": kind,
            "title": TITLES[kind],
            "best_of": g.mine.best_of,
            "host": g.mine.host == self.me,
            "seat": my_seat,
            "mark": MARKS[my_seat],
            "opponent": {"peer_id": opponent_id, "name": self._opponent_name(g), "online": bool(peer and peer.online)},
            "status": out.status,
            "result": out.result,
            "reason": out.reason,
            "your_turn": False,
            "created": g.created,
        }
        if kind == RPS:
            rounds = out.rounds or Rounds()
            current = rounds.current
            locked = len(g.mine.commits) > current
            their_commits = len(g.theirs.commits) if g.theirs else 0
            view["your_turn"] = playing and not locked
            view["rps"] = {
                "rounds": [
                    {"mine": r[my_seat], "theirs": r[them],
                     "result": "draw" if r[2] is None else ("won" if r[2] == my_seat else "lost")}
                    for r in rounds.rounds
                ],
                "score": [rounds.score[my_seat], rounds.score[them]],
                "target": g.mine.best_of // 2 + 1,
                "locked": locked and playing,
                "choice": g.choices.get(current) if locked and playing else None,
                "they_locked": playing and their_commits > current,
            }
        elif kind == BATTLESHIP:
            view.update(self._battle_view(g, out, my_seat))
        elif kind in (TYPING, MATH):
            view.update(self._race_view(g, out, my_seat))
        else:
            engine = out.engine or replay_turns(new_engine(kind, g.id), [], [])
            your_turn = playing and engine.turn == my_seat
            view["your_turn"] = your_turn
            legal = engine.legal() if your_turn else []
            view["allowed"] = [list(m) for m in legal] if kind == CHECKERS else legal
            if kind in (TICTACTOE, ULTIMATE):
                view.update(board=engine.cells, line=list(engine.line), last=engine.last)
                if engine.ultimate:
                    view.update(small=engine.small, forced=engine.forced)
            elif kind == CONNECT4:
                view.update(board=engine.cells, line=list(engine.line), last=engine.last)
            elif kind == CHECKERS:
                pieces = [sum(1 for p in engine.board if p.lower() == m) for m in MARKS]
                view.update(board=engine.board, last=list(engine.last), score=[pieces[my_seat], pieces[them]])
            elif kind == DOTS:
                view.update(lines=engine.lines, boxes=engine.boxes, last=engine.last,
                            score=[engine.score[my_seat], engine.score[them]])
            else:  # memory
                visible = set(i for i in range(len(engine.layout)) if engine.matched[i]) | set(engine.shown)
                if engine.pending is not None:
                    visible.add(engine.pending)
                if out.status == OVER:
                    visible = set(range(len(engine.layout)))
                view.update(cards=[engine.layout[i] if i in visible else None for i in range(len(engine.layout))],
                            matched=engine.matched, pending=engine.pending, shown=list(engine.shown), last=engine.last,
                            score=[engine.score[my_seat], engine.score[them]])
        return view

    def _battle_view(self, g: Game, out: Outcome, my_seat: int) -> dict:
        theirs = g.theirs
        battle = out.battle
        ready, they_ready = bool(g.mine.fleet), bool(theirs and theirs.fleet)
        phase = "setup" if not (ready and they_ready) else ("over" if out.status == OVER else "battle")
        my_ships = ship_cells(g.fleet) if g.fleet else []
        their_shots = theirs.shots if theirs else []
        incoming = [{"cell": c, "result": g.mine.answers[k] if k < len(g.mine.answers) else None}
                    for k, c in enumerate(their_shots[:battle.fired[1 - my_seat]] if battle else [])]
        outgoing = [{"cell": c, "result": theirs.answers[k] if theirs and k < len(theirs.answers) else None}
                    for k, c in enumerate(g.mine.shots[:battle.fired[my_seat]] if battle else [])]
        their_ships = []
        if theirs and theirs.reveal:
            layout = parse_layout(theirs.reveal[0])
            their_ships = ship_cells(layout) if layout else []
        sunk = [sum(1 for s in shots if s["result"] == "sunk") for shots in (outgoing, incoming)]
        return {
            "your_turn": out.status == PLAYING and phase == "battle" and battle is not None and battle.turn == my_seat,
            "battle": {
                "phase": phase,
                "ready": ready,
                "they_ready": they_ready,
                "ships": my_ships,
                "ship_names": list(SHIP_NAMES),
                "incoming": incoming,
                "outgoing": outgoing,
                "their_ships": their_ships,
                "sunk": sunk,  # ships I sank, ships I lost
                "fleet": len(FLEET),
            },
        }

    def _race_view(self, g: Game, out: Outcome, my_seat: int) -> dict:
        mine = g.mine.times
        theirs = g.theirs.times if g.theirs else []
        playing = out.status == PLAYING
        if g.mine.kind == TYPING:
            return {
                "your_turn": playing and not mine,
                "race": {
                    "sentence": sentence_for(g.id),
                    "my_time": mine[0] if mine else None,
                    "they_done": bool(theirs),
                    "their_time": theirs[0] if theirs and mine else None,
                },
            }
        race = out.race or Race()
        return {
            "your_turn": playing and len(mine) < MATH_PROBLEMS,
            "sprint": {
                "problems": [{"question": q, "answer": a} for q, a in math_problems(g.id)],
                "my_times": mine,
                "their_count": len(theirs),
                "their_times": theirs[:len(mine)],  # only the sums you've done too
                "score": [race.points[my_seat], race.points[1 - my_seat]],
            },
        }

    # ----- what the local player does ---------------------------------------------------------------

    def _get(self, game_id: str) -> Game:
        g = self._games.get(game_id)
        if g is None or g.closed_at is not None:
            raise PlayError("No such game.", 404)
        return g

    def invite(self, peer_id: str, kind: str, best_of: int = 3, first: bool = True) -> dict:
        if kind not in TITLES:
            raise PlayError("Unknown game.", 400)
        if kind == RPS and best_of not in BEST_OF:
            raise PlayError("Best of 1, 3, 5 or 7.", 400)
        peer = self.peers.get(peer_id)
        if peer is None or not peer.online:
            raise PlayError("That player isn't online.", 404)
        if not peer.play:
            raise PlayError(f"{peer.name} has an older LightningLAN without games.", 409)
        if kind not in (peer.games if peer.games is not None else {TICTACTOE, ULTIMATE, RPS}):
            raise PlayError(f"{peer.name}'s LightningLAN doesn't have {TITLES[kind]} yet.", 409)
        with self._lock:
            self._make_room()
            players = (self.me, peer_id) if first else (peer_id, self.me)
            names = (self.own_name(), peer.name) if first else (peer.name, self.own_name())
            record = Record(secrets.token_hex(16), kind, players, names, self.me,
                            best_of if kind == RPS else 1, OFFERED)
            g = Game(record)
            self._games[record.id] = g
            self._changed()
            log.info("invited %s to %s", peer.name, TITLES[kind])
            return self._game_view(g)

    def _make_room(self) -> None:
        if len(self._games) < MAX_GAMES:
            return
        finished = sorted((g for g in self._games.values() if g.closed_at is not None or self.outcome(g).status != PLAYING),
                          key=lambda g: g.created)
        if not finished:
            raise PlayError("Too many games at once: finish or close some first.", 409)
        del self._games[finished[0].id]

    def answer(self, game_id: str, accept: bool) -> dict | None:
        with self._lock:
            invite = self._invites.pop(game_id, None)
            if invite is None:
                raise PlayError("That invitation isn't open any more.", 404)
            self._make_room()
            mine = Record(invite.id, invite.kind, invite.players, invite.names, invite.host, invite.best_of,
                          ACCEPTED if accept else DECLINED)
            g = Game(mine, theirs=invite)
            if not accept:
                g.closed_at = time.time()  # nothing to show; published so the host hears the answer
            self._games[game_id] = g
            self._changed()
            return self._game_view(g) if accept else None

    def withdraw(self, game_id: str) -> None:
        with self._lock:
            g = self._get(game_id)
            if g.mine.host != self.me or self.outcome(g).status != WAITING:
                raise PlayError("Only an unanswered invitation can be withdrawn.")
            g.mine.stance = CANCELED
            g.closed_at = time.time()
            self._changed()

    def resign(self, game_id: str) -> dict:
        with self._lock:
            g = self._get(game_id)
            if self.outcome(g).status != PLAYING:
                raise PlayError("That game isn't being played.")
            g.mine.stance = RESIGNED
            self._changed()
            return self._game_view(g)

    def close(self, game_id: str) -> None:
        with self._lock:
            g = self._get(game_id)
            status = self.outcome(g).status
            if status == PLAYING:
                raise PlayError("Resign first, or finish the game.")
            if status == WAITING:
                g.mine.stance = CANCELED
            g.closed_at = time.time()
            self._changed()

    def _playing(self, game_id: str, kinds: set[str]) -> tuple[Game, Outcome]:
        g = self._get(game_id)
        if g.mine.kind not in kinds:
            raise PlayError("That's not a move in this game.", 400)
        out = self.outcome(g)
        if out.status != PLAYING:
            raise PlayError("That game isn't being played.")
        return g, out

    def move(self, game_id: str, move) -> dict:
        """A turn in tic-tac-toe, ultimate, four in a row (a column), dots and boxes (a line),
        memory match (a card), or checkers (a path of squares)."""
        with self._lock:
            g, out = self._playing(game_id, TURN_KINDS)
            engine = out.engine
            if engine.turn != g.seat_of(self.me):
                raise PlayError("It's not your turn.")
            if g.mine.kind == CHECKERS:
                if not isinstance(move, list) or tuple(move) not in engine.legal():
                    raise PlayError("You can't move there.", 400)
                move = list(move)
            elif not _is_int(move) or move not in engine.legal():
                raise PlayError("You can't play there.", 400)
            g.mine.moves.append(move)
            self._changed()
            return self._game_view(g)

    def choose(self, game_id: str, choice: str) -> dict:
        with self._lock:
            g, out = self._playing(game_id, {RPS})
            if choice not in CHOICES:
                raise PlayError("Choose rock, paper or scissors.", 400)
            current = out.rounds.current if out.rounds else 0
            if len(g.mine.commits) != current:
                raise PlayError("You've already locked in this round.")
            salt = secrets.token_hex(16)
            g.secrets[current] = [choice, salt]
            g.choices[current] = choice
            g.mine.commits.append(seal(g.id, current, choice, salt))
            self._respond(g)
            self._changed()
            return self._game_view(g)

    def shuffle_fleet(self, game_id: str) -> dict:
        with self._lock:
            g, _ = self._playing(game_id, {BATTLESHIP})
            if g.mine.fleet:
                raise PlayError("Your ships are already in place.")
            g.fleet = random_layout(_random)
            self._changed()
            return self._game_view(g)

    def fleet_ready(self, game_id: str) -> dict:
        with self._lock:
            g, _ = self._playing(game_id, {BATTLESHIP})
            if g.mine.fleet:
                raise PlayError("Your ships are already in place.")
            if g.fleet is None:
                g.fleet = random_layout(_random)
            g.fleet_salt = secrets.token_hex(16)
            g.mine.fleet = seal_fleet(g.id, layout_text(g.fleet), g.fleet_salt)
            self._respond(g)
            self._changed()
            return self._game_view(g)

    def fire(self, game_id: str, cell: int) -> dict:
        with self._lock:
            g, out = self._playing(game_id, {BATTLESHIP})
            battle = out.battle
            if battle is None:
                raise PlayError("Both fleets have to be in place first.")
            if battle.turn != g.seat_of(self.me):
                raise PlayError("It's not your turn.")
            if not _is_int(cell) or not 0 <= cell < 100 or cell in g.mine.shots:
                raise PlayError("Pick a square you haven't fired at.", 400)
            g.mine.shots.append(cell)
            self._changed()
            return self._game_view(g)

    def finish(self, game_id: str, time_ms: int) -> dict:
        """Typing race: the time for the sentence. Math sprint: the time for the next sum."""
        with self._lock:
            g, _ = self._playing(game_id, {TYPING, MATH})
            low, high = TYPING_TIMES if g.mine.kind == TYPING else MATH_TIMES
            if not _is_int(time_ms) or not low <= time_ms <= high:
                raise PlayError("That time doesn't look right.", 400)
            if len(g.mine.times) >= (1 if g.mine.kind == TYPING else MATH_PROBLEMS):
                raise PlayError("You've already finished.")
            g.mine.times.append(time_ms)
            self._changed()
            return self._game_view(g)

    def rematch(self, game_id: str) -> dict:
        with self._lock:
            g = self._get(game_id)
            if self.outcome(g).status not in (OVER, DECLINED, CANCELED):
                raise PlayError("Finish this game first.")
            opponent = self._opponent_id(g)
            first = g.mine.players[0] != self.me  # take turns going first
            kind, best_of = g.mine.kind, g.mine.best_of
            g.closed_at = time.time()
            self._changed()
        return self.invite(opponent, kind, best_of, first)

    def _respond(self, g: Game) -> None:
        """What this side publishes by itself once the other side has done something: rock-paper-scissors
        choices once both have locked in, and battleship answers to shots and the fleet once it's over."""
        theirs = g.theirs
        if theirs is None:
            return
        if g.mine.kind == RPS:
            while True:
                r = len(g.mine.reveals)
                if r >= len(g.mine.commits) or r >= len(theirs.commits) or r not in g.secrets:
                    break
                g.mine.reveals.append(g.secrets.pop(r))
                self._changed()
        elif g.mine.kind == BATTLESHIP and g.mine.fleet and g.fleet:
            answers = answer_shots(ship_cells(g.fleet), theirs.shots)
            if answers != g.mine.answers:
                g.mine.answers = answers
                self._changed()
            if g.mine.reveal is None:
                first, second = self._halves(g)
                if first and second and first.fleet and second.fleet:
                    battle = replay_battle([first.shots, second.shots], [first.answers, second.answers])
                    if battle.winner is not None:
                        g.mine.reveal = [layout_text(g.fleet), g.fleet_salt]
                        self._changed()

    # ----- polling opponents --------------------------------------------------------------------------

    def _involved_with(self, peer_id: str) -> bool:
        """Whether a game with this peer is under way (then it is polled quickly)."""
        if any(r.host == peer_id for r in self._invites.values()):
            return True
        for g in self._games.values():
            if self._opponent_id(g) == peer_id and g.closed_at is None and self.outcome(g).status in (WAITING, PLAYING):
                return True
        return False

    async def _run(self) -> None:
        while True:
            try:
                now = time.monotonic()
                for peer in self.peers.online():
                    if not peer.play or peer.peer_id in self._inflight:
                        continue
                    with self._lock:
                        quick = self._involved_with(peer.peer_id)
                    interval = ACTIVE_POLL if quick else self.refresh_interval
                    if now - self._polled.get(peer.peer_id, 0.0) >= interval:
                        self._polled[peer.peer_id] = now
                        self._inflight.add(peer.peer_id)
                        task = asyncio.create_task(self._poll(peer.peer_id, peer.base_url()))
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)
                self._housekeeping()
            except Exception:
                log.exception("game polling failed")
            await asyncio.sleep(0.2)

    async def _poll(self, peer_id: str, base_url: str) -> None:
        try:
            assert self._client is not None
            path = "/api/games"
            headers = self.auth.sign_request("GET", path)
            etag = self._etags.get(peer_id)
            if etag:
                headers["If-None-Match"] = etag
            async with self._client.stream("GET", base_url + path, headers=headers) as resp:
                if resp.status_code != 200:
                    return
                if not self.auth.verify_response(headers, resp.headers):
                    return
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_PAYLOAD:
                        return
                new_etag = resp.headers.get("etag")
            payload = json.loads(body)
            self.merge(peer_id, payload)
            if new_etag:
                self._etags[peer_id] = new_etag
        except (httpx.HTTPError, ValueError) as exc:
            log.debug("could not read %s's games: %s", peer_id[:8], exc)
        finally:
            self._inflight.discard(peer_id)

    def merge(self, peer_id: str, payload: object) -> None:
        """Take in what a peer publishes about its games (only its own half of each counts)."""
        if not isinstance(payload, dict) or payload.get("peer_id") != peer_id:
            return
        raw = payload.get("games")
        if not isinstance(raw, list):
            return
        with self._lock:
            offered = set()
            for item in raw[:MAX_GAMES * 2]:
                record = parse_record(item)
                if record is None or peer_id not in record.players or self.me not in record.players:
                    continue
                g = self._games.get(record.id)
                if g is not None:
                    if g.mine.same_game(record) and self._opponent_id(g) == peer_id and _differs(g.theirs, record):
                        g.theirs = record
                        self._respond(g)
                        self._changed()
                    continue
                if record.host == peer_id and record.stance == OFFERED:
                    offered.add(record.id)
                    if record.id not in self._invites:
                        self._invites[record.id] = record
                        self._changed()
                        log.info("%s invited us to %s", record.names[record.players.index(peer_id)], TITLES[record.kind])
            for game_id, record in list(self._invites.items()):
                if record.host == peer_id and game_id not in offered:
                    del self._invites[game_id]  # withdrawn
                    self._changed()

    def _housekeeping(self) -> None:
        now = time.time()
        with self._lock:
            for g in list(self._games.values()):
                if g.closed_at is not None and now - g.closed_at > KEEP_CLOSED:
                    del self._games[g.id]
                    self._changed()
                elif (g.mine.host == self.me and g.mine.stance == OFFERED and now - g.created > INVITE_TTL
                      and self.outcome(g).status == WAITING):
                    g.mine.stance = CANCELED  # nobody answered
                    self._changed()
            online = {p.peer_id for p in self.peers.online()}
            for game_id, record in list(self._invites.items()):
                if record.host not in online:
                    del self._invites[game_id]
                    self._changed()


def _differs(a: Record | None, b: Record) -> bool:
    return a is None or a.to_dict() != b.to_dict()
