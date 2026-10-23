"""The rules of the head-to-head games (While you wait), and the content they share.

Both players' LightningLAN replay every game from the two players' moves with these rules, so
they must give the same answer everywhere: the Android app's Games.kt is a line-by-line port.
Anything random (the memory cards, the typing sentence, the sums) comes from the game's ID through
SHA-256, never from a random number generator, for the same reason.
"""

from __future__ import annotations

import hashlib
import hmac
import random
from dataclasses import dataclass, field

MARKS = ("x", "o")  # seat 0 and seat 1; seat 0 always moves first


def _digest(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def replay_turns(game, first: list, second: list):
    """Feed each player's moves to the game whenever the rules say it's their turn.

    A player's move only counts in turn, and nothing after an illegal move counts.
    """
    lists = (first, second)
    used = [0, 0]
    while not game.over:
        seat = game.turn
        if used[seat] >= len(lists[seat]) or not game.play(lists[seat][used[seat]]):
            break
        used[seat] += 1
    return game


def interleave(first: list[int], second: list[int]) -> list[int]:
    """The moves of a game where the players simply alternate, in playing order."""
    order: list[int] = []
    for i, move in enumerate(first):
        order.append(move)
        if i >= len(second):
            break
        order.append(second[i])
    return order


# ----- tic-tac-toe and ultimate tic-tac-toe ---------------------------------------------------------

LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))


def line_of(cells: list[str]) -> tuple[str, tuple[int, int, int]] | None:
    for line in LINES:
        a, b, c = line
        if cells[a] in MARKS and cells[a] == cells[b] == cells[c]:
            return cells[a], line
    return None


@dataclass
class Board:
    """Tic-tac-toe, or ultimate tic-tac-toe (nine boards; your move picks the board your opponent plays next)."""

    ultimate: bool
    cells: list[str]
    small: list[str] = field(default_factory=lambda: [""] * 9)  # ultimate: "x", "o", "draw" or ""
    turn: int = 0
    winner: int | None = None
    draw: bool = False
    line: tuple[int, ...] = ()
    last: int | None = None
    forced: int | None = None  # ultimate: the small board the next move must be in, if any

    @property
    def over(self) -> bool:
        return self.winner is not None or self.draw

    def legal(self) -> list[int]:
        if self.over:
            return []
        if not self.ultimate:
            return [i for i in range(9) if not self.cells[i]]
        boards = [self.forced] if self.forced is not None else [b for b in range(9) if not self.small[b]]
        return [b * 9 + c for b in boards for c in range(9) if not self.cells[b * 9 + c]]

    def play(self, move: int) -> bool:
        if move not in self.legal():
            return False
        mark = MARKS[self.turn]
        self.cells[move] = mark
        self.last = move
        if self.ultimate:
            b, c = divmod(move, 9)
            if line_of(self.cells[b * 9:b * 9 + 9]):
                self.small[b] = mark
            elif all(self.cells[b * 9:b * 9 + 9]):
                self.small[b] = "draw"
            overall = line_of(self.small)
            if overall:
                self.winner, self.line = self.turn, overall[1]
            elif all(self.small):
                self.draw = True
            self.forced = None if self.small[c] else c
        else:
            won = line_of(self.cells)
            if won:
                self.winner, self.line = self.turn, won[1]
            elif all(self.cells):
                self.draw = True
        self.turn = 1 - self.turn
        return True


def replay_board(ultimate: bool, first: list[int], second: list[int]) -> Board:
    return replay_turns(Board(ultimate, [""] * (81 if ultimate else 9)), first, second)


# ----- four in a row ----------------------------------------------------------------------------------

C4_ROWS, C4_COLS = 6, 7


def four_in_a_row(cells: list[str]) -> tuple[int, ...] | None:
    """The first four in a row on the board (scanning rows, then columns), as cell indexes."""
    for r in range(C4_ROWS):
        for c in range(C4_COLS):
            mark = cells[r * C4_COLS + c]
            if not mark:
                continue
            for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
                end_r, end_c = r + 3 * dr, c + 3 * dc
                if not (0 <= end_r < C4_ROWS and 0 <= end_c < C4_COLS):
                    continue
                run = tuple((r + k * dr) * C4_COLS + c + k * dc for k in range(4))
                if all(cells[i] == mark for i in run):
                    return run
    return None


@dataclass
class FourInARow:
    """Drop discs into seven columns; four in a row wins. A move is a column (0-6)."""

    cells: list[str] = field(default_factory=lambda: [""] * (C4_ROWS * C4_COLS))  # row 0 is the top
    turn: int = 0
    winner: int | None = None
    draw: bool = False
    line: tuple[int, ...] = ()
    last: int | None = None

    @property
    def over(self) -> bool:
        return self.winner is not None or self.draw

    def legal(self) -> list[int]:
        return [] if self.over else [c for c in range(C4_COLS) if not self.cells[c]]

    def play(self, column: int) -> bool:
        if column not in self.legal():
            return False
        row = max(r for r in range(C4_ROWS) if not self.cells[r * C4_COLS + column])
        cell = row * C4_COLS + column
        self.cells[cell] = MARKS[self.turn]
        self.last = cell
        line = four_in_a_row(self.cells)
        if line:
            self.winner, self.line = self.turn, line
        elif all(self.cells):
            self.draw = True
        self.turn = 1 - self.turn
        return True


# ----- checkers ---------------------------------------------------------------------------------------

CHECKERS_QUIET_LIMIT = 80  # turns in a row without a capture: a draw


def _seat_of(piece: str) -> int | None:
    if piece in ("x", "X"):
        return 0
    if piece in ("o", "O"):
        return 1
    return None


def _directions(piece: str) -> tuple[tuple[int, int], ...]:
    if piece == "x":
        return ((-1, -1), (-1, 1))  # seat 0 starts at the bottom and moves up
    if piece == "o":
        return ((1, -1), (1, 1))
    return ((-1, -1), (-1, 1), (1, -1), (1, 1))  # kings


def _crowned(piece: str, row: int) -> bool:
    return (piece == "x" and row == 0) or (piece == "o" and row == 7)


@dataclass
class Checkers:
    """English draughts on the 8x8 board's dark squares (index row * 8 + column).

    Men move one square diagonally forward; kings either way. Jumping is compulsory, and a jump
    must go on while it can: a move is the whole path, e.g. [41, 27, 13] for a double jump. A man
    reaching the far row is crowned, which ends the move. You lose when you can't move.
    """

    board: list[str] = field(default_factory=lambda: [
        ("o" if r <= 2 else "x" if r >= 5 else "") if (r + c) % 2 == 1 else ""
        for r in range(8) for c in range(8)
    ])
    turn: int = 0
    winner: int | None = None
    draw: bool = False
    quiet: int = 0
    last: tuple[int, ...] = ()
    _legal: list[tuple[int, ...]] | None = None

    @property
    def over(self) -> bool:
        return self.winner is not None or self.draw

    def legal(self) -> list[tuple[int, ...]]:
        if self.over:
            return []
        if self._legal is None:
            self._legal = self._moves()
        return self._legal

    def _moves(self) -> list[tuple[int, ...]]:
        jumps: list[tuple[int, ...]] = []
        steps: list[tuple[int, ...]] = []
        for square in range(64):
            piece = self.board[square]
            if _seat_of(piece) != self.turn:
                continue
            jumps.extend(self._jumps(square, piece))
            r, c = divmod(square, 8)
            for dr, dc in _directions(piece):
                tr, tc = r + dr, c + dc
                if 0 <= tr < 8 and 0 <= tc < 8 and not self.board[tr * 8 + tc]:
                    steps.append((square, tr * 8 + tc))
        return jumps or steps

    def _jumps(self, start: int, piece: str) -> list[tuple[int, ...]]:
        found: list[tuple[int, ...]] = []

        def walk(square: int, path: list[int], taken: frozenset[int]) -> None:
            r, c = divmod(square, 8)
            went_on = False
            for dr, dc in _directions(piece):
                lr, lc = r + 2 * dr, c + 2 * dc
                if not (0 <= lr < 8 and 0 <= lc < 8):
                    continue
                middle, landing = (r + dr) * 8 + c + dc, lr * 8 + lc
                if middle in taken or _seat_of(self.board[middle]) != 1 - self.turn:
                    continue
                if self.board[landing] and landing != start:
                    continue
                went_on = True
                if _crowned(piece, lr):
                    found.append(tuple(path + [landing]))  # crowned: the move ends here
                else:
                    walk(landing, path + [landing], taken | {middle})
            if not went_on and len(path) > 1:
                found.append(tuple(path))

        walk(start, [start], frozenset())
        return found

    def play(self, path) -> bool:
        if not isinstance(path, (list, tuple)) or tuple(path) not in self.legal():
            return False
        path = tuple(path)
        piece = self.board[path[0]]
        self.board[path[0]] = ""
        captured = False
        for a, b in zip(path, path[1:]):
            if abs(b // 8 - a // 8) == 2:
                self.board[(a + b) // 2] = ""
                captured = True
        if _crowned(piece, path[-1] // 8):
            piece = piece.upper()
        self.board[path[-1]] = piece
        self.last = path
        self.quiet = 0 if captured else self.quiet + 1
        self.turn = 1 - self.turn
        self._legal = None
        if not self.legal():
            self.winner = 1 - self.turn  # the player to move can't: the other one wins
        elif self.quiet >= CHECKERS_QUIET_LIMIT:
            self.draw = True
        return True


# ----- dots and boxes --------------------------------------------------------------------------------

DOTS_SIZE = 5  # boxes per side: 6 x 6 dots, 60 lines
DOTS_LINES = 2 * DOTS_SIZE * (DOTS_SIZE + 1)


def box_lines(box: int) -> tuple[int, int, int, int]:
    """Top, bottom, left and right line of a box. Horizontal lines come first (row * 5 + column),
    then vertical ones (30 + row * 6 + column)."""
    r, c = divmod(box, DOTS_SIZE)
    horizontal = DOTS_SIZE * (DOTS_SIZE + 1)
    return (r * DOTS_SIZE + c, (r + 1) * DOTS_SIZE + c,
            horizontal + r * (DOTS_SIZE + 1) + c, horizontal + r * (DOTS_SIZE + 1) + c + 1)


def boxes_of(line: int) -> list[int]:
    horizontal = DOTS_SIZE * (DOTS_SIZE + 1)
    if line < horizontal:
        r, c = divmod(line, DOTS_SIZE)
        return [b for b, ok in (((r - 1) * DOTS_SIZE + c, r > 0), (r * DOTS_SIZE + c, r < DOTS_SIZE)) if ok]
    r, c = divmod(line - horizontal, DOTS_SIZE + 1)
    return [b for b, ok in ((r * DOTS_SIZE + c - 1, c > 0), (r * DOTS_SIZE + c, c < DOTS_SIZE)) if ok]


@dataclass
class Dots:
    """Draw lines between dots; closing a box claims it and earns another turn. A move is a line."""

    lines: list[str] = field(default_factory=lambda: [""] * DOTS_LINES)
    boxes: list[str] = field(default_factory=lambda: [""] * (DOTS_SIZE * DOTS_SIZE))
    score: list[int] = field(default_factory=lambda: [0, 0])
    turn: int = 0
    winner: int | None = None
    draw: bool = False
    last: int | None = None

    @property
    def over(self) -> bool:
        return self.winner is not None or self.draw

    def legal(self) -> list[int]:
        return [] if self.over else [i for i in range(DOTS_LINES) if not self.lines[i]]

    def play(self, line: int) -> bool:
        if line not in self.legal():
            return False
        mark = MARKS[self.turn]
        self.lines[line] = mark
        self.last = line
        closed = False
        for box in boxes_of(line):
            if not self.boxes[box] and all(self.lines[i] for i in box_lines(box)):
                self.boxes[box] = mark
                self.score[self.turn] += 1
                closed = True
        if all(self.boxes):
            if self.score[0] == self.score[1]:
                self.draw = True
            else:
                self.winner = 0 if self.score[0] > self.score[1] else 1
        elif not closed:
            self.turn = 1 - self.turn
        return True


# ----- memory match -------------------------------------------------------------------------------------

MEMORY_CARDS = 16  # a 4 x 4 grid: 8 pairs


def memory_layout(game_id: str) -> list[int]:
    """Which picture (0-7) is on each card: the same shuffle on both sides, from the game's ID."""
    deck = [i // 2 for i in range(MEMORY_CARDS)]
    for i in range(MEMORY_CARDS - 1, 0, -1):
        j = int.from_bytes(_digest(f"lightninglan-memory|{game_id}|{i}")[:8], "big") % (i + 1)
        deck[i], deck[j] = deck[j], deck[i]
    return deck


@dataclass
class Memory:
    """Take turns turning over two cards; a pair is yours, and you go again. A move is one card."""

    layout: list[int]
    matched: list[str] = field(default_factory=lambda: [""] * MEMORY_CARDS)
    pending: int | None = None  # the first card of the current turn
    shown: tuple[int, ...] = ()  # the last pair that didn't match (face up until the next card)
    score: list[int] = field(default_factory=lambda: [0, 0])
    turn: int = 0
    winner: int | None = None
    draw: bool = False
    last: int | None = None

    @property
    def over(self) -> bool:
        return self.winner is not None or self.draw

    def legal(self) -> list[int]:
        if self.over:
            return []
        return [i for i in range(MEMORY_CARDS) if not self.matched[i] and i != self.pending]

    def play(self, card: int) -> bool:
        if card not in self.legal():
            return False
        self.last = card
        if self.pending is None:
            self.pending = card
            self.shown = ()
            return True
        first, self.pending = self.pending, None
        if self.layout[first] == self.layout[card]:
            self.matched[first] = self.matched[card] = MARKS[self.turn]
            self.score[self.turn] += 1
            self.shown = ()
            if all(self.matched):
                if self.score[0] == self.score[1]:
                    self.draw = True
                else:
                    self.winner = 0 if self.score[0] > self.score[1] else 1
        else:
            self.shown = (first, card)
            self.turn = 1 - self.turn
        return True


# ----- rock-paper-scissors --------------------------------------------------------------------------------

CHOICES = ("rock", "paper", "scissors")
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
MAX_ROUNDS = 99  # a run of draws ends the match as a draw


def seal(game_id: str, round_no: int, choice: str, salt: str) -> str:
    """The hash a rock-paper-scissors choice is locked in with, before anyone reveals."""
    return hashlib.sha256(f"lightninglan-rps|{game_id}|{round_no}|{choice}|{salt}".encode("utf-8")).hexdigest()


@dataclass
class Rounds:
    rounds: list[tuple[str, str, int | None]] = field(default_factory=list)  # (seat 0, seat 1, winner)
    score: list[int] = field(default_factory=lambda: [0, 0])
    winner: int | None = None
    draw: bool = False
    cheat: int | None = None  # the seat whose revealed choice didn't match what it locked in

    @property
    def current(self) -> int:
        return len(self.rounds)

    @property
    def over(self) -> bool:
        return self.winner is not None or self.draw


def replay_rounds(game_id: str, best_of: int, commits: list[list[str]], reveals: list[list[list[str]]]) -> Rounds:
    result = Rounds()
    target = best_of // 2 + 1
    for r in range(MAX_ROUNDS):
        if r >= min(len(commits[0]), len(commits[1])) or r >= min(len(reveals[0]), len(reveals[1])):
            return result
        picks = []
        for seat in (0, 1):
            choice, salt = reveals[seat][r]
            if not hmac.compare_digest(seal(game_id, r, choice, salt), commits[seat][r]):
                result.cheat, result.winner = seat, 1 - seat
                return result
            picks.append(choice)
        winner = None if picks[0] == picks[1] else (0 if BEATS[picks[0]] == picks[1] else 1)
        result.rounds.append((picks[0], picks[1], winner))
        if winner is not None:
            result.score[winner] += 1
            if result.score[winner] >= target:
                result.winner = winner
                return result
    result.draw = True
    return result


# ----- battleship -----------------------------------------------------------------------------------------

GRID = 10
FLEET = (5, 4, 3, 3, 2)
SHIP_NAMES = ("carrier", "battleship", "cruiser", "submarine", "destroyer")
FLEET_CELLS = sum(FLEET)  # hits that sink the whole fleet


def ship_cells(layout: list[tuple[int, bool]]) -> list[list[int]] | None:
    """The cells of each ship ((bow, horizontal) per ship, in FLEET order), or None if the layout is invalid."""
    if len(layout) != len(FLEET):
        return None
    taken: set[int] = set()
    ships = []
    for (bow, horizontal), size in zip(layout, FLEET):
        if not 0 <= bow < GRID * GRID:
            return None
        r, c = divmod(bow, GRID)
        if (c if horizontal else r) + size > GRID:
            return None
        cells = [bow + k * (1 if horizontal else GRID) for k in range(size)]
        if taken.intersection(cells):
            return None
        taken.update(cells)
        ships.append(cells)
    return ships


def layout_text(layout: list[tuple[int, bool]]) -> str:
    return ",".join(f"{bow}{'h' if horizontal else 'v'}" for bow, horizontal in layout)


def parse_layout(text: str) -> list[tuple[int, bool]] | None:
    layout = []
    for part in text.split(","):
        if len(part) < 2 or part[-1] not in "hv" or not part[:-1].isdigit() or len(part) > 4:
            return None
        layout.append((int(part[:-1]), part[-1] == "h"))
    return layout if ship_cells(layout) is not None else None


def random_layout(rng: random.Random) -> list[tuple[int, bool]]:
    while True:
        layout = [(rng.randrange(GRID * GRID), rng.random() < 0.5) for _ in FLEET]
        if ship_cells(layout) is not None:
            return layout


def seal_fleet(game_id: str, text: str, salt: str) -> str:
    """The hash a hidden fleet is locked in with; revealed and checked when the game ends."""
    return hashlib.sha256(f"lightninglan-battleship|{game_id}|{text}|{salt}".encode("utf-8")).hexdigest()


def answer_shots(ships: list[list[int]], shots: list[int]) -> list[str]:
    """What a fleet says to each shot fired at it: "miss", "hit", or "sunk" (the last hit on a ship)."""
    hits: set[int] = set()
    answers = []
    for shot in shots:
        ship = next((cells for cells in ships if shot in cells), None)
        if ship is None:
            answers.append("miss")
        else:
            hits.add(shot)
            answers.append("sunk" if all(cell in hits for cell in ship) else "hit")
    return answers


@dataclass
class Battle:
    """Where a battle stands, from both players' shots and both fleets' answers."""

    turn: int = 0
    winner: int | None = None
    hits: list[int] = field(default_factory=lambda: [0, 0])  # hits each seat's fleet took
    fired: list[int] = field(default_factory=lambda: [0, 0])  # shots that count, per seat

    @property
    def over(self) -> bool:
        return self.winner is not None


def replay_battle(shots: list[list[int]], answers: list[list[str]]) -> Battle:
    """shots[s]: what seat s fired; answers[s]: what seat s's fleet said to the other seat's shots.

    The players take turns, one shot each. The first to have the whole fleet hit loses; shots after
    that don't count.
    """
    battle = Battle()
    seen: list[set[int]] = [set(), set()]
    for i, cell in enumerate(interleave(shots[0], shots[1])):
        seat, k = i % 2, i // 2
        if cell in seen[seat]:
            break  # a square can only be shot once
        seen[seat].add(cell)
        battle.fired[seat] += 1
        target = 1 - seat
        if k < len(answers[target]) and answers[target][k] in ("hit", "sunk"):
            battle.hits[target] += 1
            if battle.hits[target] >= FLEET_CELLS:
                battle.winner = seat
                break
    battle.turn = 0 if battle.fired[0] == battle.fired[1] else 1
    return battle


# ----- typing race and math sprint -------------------------------------------------------------------------

SENTENCES = (
    "The quick brown fox jumps over the lazy dog while the files keep flowing.",
    "Lightning never strikes the same router twice, but it does download fast.",
    "A watched progress bar never fills, so type something while you wait.",
    "Seven sleepy penguins shared a pizza and a very large video file.",
    "Every byte crossed the network safely and arrived just in time for lunch.",
    "My cat walked across the keyboard and somehow sent a perfect email.",
    "Clouds gather over the mountains as the last download reaches one hundred percent.",
    "Bring snacks, charge your phone, and never trust a printer on a Monday.",
    "The wizard waved his wand and the spreadsheet finally balanced itself.",
    "Fast fingers win races, but careful fingers win them without typos.",
    "Somewhere a server hums a quiet song for all the packets passing by.",
    "Ten tiny turtles tried to tango on a tightly tuned telephone wire.",
    "If the music is too loud, you are probably standing next to the speakers.",
    "A brave little laptop carried the whole family photo album across the house.",
    "Please remember to water the plants and back up your important files.",
    "The dragon guarded a hoard of old game saves and forgotten homework.",
    "Rain tapped on the window while the movie copied from one room to another.",
    "Good passwords are long, strange, and impossible for your cousin to guess.",
    "We built a fort out of cardboard boxes and called it the data center.",
    "Thunder rolled across the valley and the dog hid under the sofa again.",
    "Never underestimate the power of a sandwich during a long road trip.",
    "The robot learned to juggle three apples, a banana, and a flashlight.",
    "Our team named the new router after a famous pirate with one eye.",
    "Cold coffee and warm cookies make a surprisingly good breakfast.",
    "The museum had a whole room full of very old and very slow computers.",
    "Quietly, the moon rose over the city and every light blinked twice.",
    "Jumping frogs and dancing goats filled the barn with joyful noise.",
    "A good game of chess can last longer than most phone batteries.",
    "Pack your bags, grab your map, and follow the blinking network light.",
    "Bright sparks flew as the old toaster finally retired after twenty years.",
)


def sentence_for(game_id: str) -> str:
    return SENTENCES[int(game_id[:8], 16) % len(SENTENCES)]


MATH_PROBLEMS = 10


def math_problems(game_id: str) -> list[tuple[str, int]]:
    """The sums of a math sprint, from the game's ID: (question, answer)."""
    problems = []
    for r in range(MATH_PROBLEMS):
        h = _digest(f"lightninglan-math|{game_id}|{r}")
        op = h[0] % 3
        if op == 0:
            a, b = 2 + h[1] % 49, 2 + h[2] % 49
            problems.append((f"{a} + {b}", a + b))
        elif op == 1:
            a = 10 + h[1] % 90
            b = 1 + h[2] % a
            problems.append((f"{a} − {b}", a - b))
        else:
            a, b = 2 + h[1] % 11, 2 + h[2] % 11
            problems.append((f"{a} × {b}", a * b))
    return problems


@dataclass
class Race:
    """A typing race or a math sprint: each player times themselves; quicker wins (per sum, in a sprint)."""

    points: list[int] = field(default_factory=lambda: [0, 0])
    rounds: list[int | None] = field(default_factory=list)  # who won each scored round (None: a tie)
    winner: int | None = None
    draw: bool = False

    @property
    def over(self) -> bool:
        return self.winner is not None or self.draw


def replay_race(times: list[list[int]], rounds: int) -> Race:
    race = Race()
    for r in range(min(len(times[0]), len(times[1]), rounds)):
        a, b = times[0][r], times[1][r]
        won = None if a == b else (0 if a < b else 1)
        race.rounds.append(won)
        if won is not None:
            race.points[won] += 1
    if len(race.rounds) == rounds:
        if race.points[0] == race.points[1]:
            race.draw = True
        else:
            race.winner = 0 if race.points[0] > race.points[1] else 1
    return race
