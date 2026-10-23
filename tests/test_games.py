"""The rules of the head-to-head games (lightninglan/games.py)."""

import random

from lightninglan import games
from lightninglan.games import (
    Checkers, Dots, FourInARow, Memory, answer_shots, box_lines, boxes_of, layout_text, math_problems,
    memory_layout, parse_layout, random_layout, replay_battle, replay_race, replay_turns, sentence_for, ship_cells,
)

GID = "0123456789abcdef" * 2


def test_four_in_a_row_drops_discs_and_finds_lines():
    game = replay_turns(FourInARow(), [3, 3, 3, 3], [4, 4, 4])
    assert game.winner == 0 and game.line == (17, 24, 31, 38)  # a column (rows 2-5)
    game = replay_turns(FourInARow(), [0, 1, 2, 3], [0, 1, 2])
    assert game.winner == 0 and sorted(game.line) == [35, 36, 37, 38]  # along the bottom row
    diagonal = replay_turns(FourInARow(), [0, 1, 2, 3, 2, 3], [1, 2, 3, 6, 3])
    assert diagonal.winner == 0 and diagonal.line == (17, 23, 29, 35)  # rising from the bottom left
    full = FourInARow()
    for _ in range(3):
        full.play(0)
        full.play(0)
    assert 0 not in full.legal() and not full.play(0)


def test_checkers_start_and_simple_moves():
    game = Checkers()
    assert game.board.count("x") == 12 and game.board.count("o") == 12
    assert len(game.legal()) == 7  # seat 0's front row: 4 men, the edge one has one move
    assert all(abs(b // 8 - a // 8) == 1 for a, b in game.legal())
    assert game.play([40, 33]) and game.turn == 1 and game.board[33] == "x"


def blank_checkers(turn=0):
    game = Checkers()
    game.board = [""] * 64
    game.turn = turn
    game._legal = None
    return game


def test_checkers_jumps_are_compulsory_and_chain():
    game = blank_checkers()
    game.board[45] = "x"  # row 5, column 5
    game.board[36] = "o"  # diagonally in front
    game.board[20] = "o"  # and again after the first jump lands on 27
    game.board[60] = "x"  # another man that could just step
    game.board[7] = "o"  # keeps seat 1 alive afterwards
    assert game.legal() == [(45, 27, 13)]  # the double jump is the only legal move
    assert not game.play([60, 51])  # stepping isn't allowed while a jump is possible
    assert not game.play([45, 27])  # nor stopping half way
    assert game.play([45, 27, 13])
    assert game.board[36] == game.board[20] == "" and game.board[13] == "x" and game.turn == 1


def test_checkers_crowning_ends_the_move_and_kings_go_backwards():
    game = blank_checkers()
    game.board[19] = "x"  # row 2
    game.board[10] = "o"  # row 1: jumping it lands on row 0
    game.board[3] = ""
    game.board[63] = "o"
    assert (19, 1) in game.legal()
    assert game.play([19, 1]) and game.board[1] == "X"
    king = blank_checkers()
    king.board[27] = "X"
    king.board[63] = "o"
    assert {(27, 18), (27, 20), (27, 34), (27, 36)} <= set(king.legal())


def test_checkers_you_lose_when_you_cant_move():
    game = blank_checkers()
    game.board[45] = "x"
    game.board[36] = "o"  # seat 1's last piece
    assert game.play([45, 27]) and game.winner == 0


def test_dots_and_boxes_closing_a_box_gives_another_turn():
    assert box_lines(0) == (0, 5, 30, 31) and sorted(boxes_of(5)) == [0, 5] and boxes_of(30) == [0]
    game = Dots()
    for line in (0, 5, 30):  # three sides of box 0, by alternating players
        assert game.play(line)
    assert game.turn == 1
    assert game.play(31) and game.boxes[0] == "o" and game.score == [0, 1] and game.turn == 1  # o goes again
    assert game.play(59) and game.turn == 0


def test_dots_and_boxes_ends_when_every_box_is_claimed():
    game = Dots()
    order = list(range(games.DOTS_LINES))
    while not game.over:
        assert game.play(next(line for line in order if not game.lines[line]))
    assert sum(game.score) == 25 and game.winner in (0, 1)


def test_memory_layout_is_the_same_shuffle_everywhere():
    layout = memory_layout(GID)
    assert sorted(layout) == [i // 2 for i in range(16)]
    assert layout == memory_layout(GID) and layout != memory_layout("f" * 32)
    assert layout == [2, 4, 7, 5, 1, 3, 0, 4, 6, 2, 6, 7, 3, 1, 0, 5]  # also checked by the Android app


def test_memory_pairs_keep_the_turn_and_misses_pass_it():
    game = Memory([0, 0, 1, 1] + [2 + i // 2 for i in range(12)])
    assert game.play(0) and game.pending == 0 and game.turn == 0
    assert game.play(1) and game.matched[:2] == ["x", "x"] and game.turn == 0  # a pair: again
    assert game.play(2) and game.play(4)  # a miss
    assert game.shown == (2, 4) and game.turn == 1
    assert not game.play(0)  # matched cards are out of the game


def test_battleship_fleets_answers_and_turns():
    layout = [(0, True), (20, True), (40, True), (60, True), (80, True)]
    ships = ship_cells(layout)
    assert ships[0] == [0, 1, 2, 3, 4] and ships[4] == [80, 81]
    assert parse_layout(layout_text(layout)) == layout and layout_text(layout) == "0h,20h,40h,60h,80h"
    assert ship_cells([(8, True)] + layout[1:]) is None  # off the edge
    assert ship_cells([(0, False)] + layout[1:]) is None  # crosses the next ship
    assert parse_layout("0h,20h") is None
    assert ship_cells(random_layout(random.Random(1))) is not None
    assert answer_shots(ships, [5, 80, 81, 0]) == ["miss", "hit", "sunk", "hit"]
    battle = replay_battle([[5, 6], [7]], [["miss"], ["miss", "hit"]])
    assert battle.fired == [2, 1] and battle.turn == 1 and battle.hits == [0, 1]  # seat 1's fleet was hit
    battle = replay_battle([[1, 1], [7, 8]], [["miss", "miss"], ["miss", "miss"]])
    assert battle.fired == [1, 1]  # the same square twice doesn't count


def test_math_sprint_and_typing_race_come_from_the_game_id():
    problems = math_problems(GID)
    assert len(problems) == 10 and problems == math_problems(GID)
    for question, answer in problems:
        a, op, b = question.split(" ")
        assert answer == {"+": int(a) + int(b), "−": int(a) - int(b), "×": int(a) * int(b)}[op]
    assert problems[:3] == [("57 − 23", 34), ("11 × 12", 132), ("8 × 8", 64)]  # also checked on Android
    assert sentence_for(GID) == games.SENTENCES[13]


def test_races_compare_times():
    race = replay_race([[900, 1200, 800], [1000, 1000, 800]], 3)
    assert race.rounds == [0, 1, None] and race.points == [1, 1] and race.draw
    assert replay_race([[2000], []], 1).over is False
    assert replay_race([[2000], [1500]], 1).winner == 1
