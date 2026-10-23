package lightninglan.core

import java.nio.ByteBuffer
import java.security.MessageDigest
import kotlin.random.Random

/*
 * The rules of the head-to-head games (While you wait): a line-by-line port of the desktop app's
 * lightninglan/games.py. Both players replay every game with these, so they must agree exactly.
 * Anything random (memory cards, the typing sentence, the sums) comes from the game's ID through
 * SHA-256, never from a random number generator.
 */

val MARKS = listOf("x", "o") // seat 0 and seat 1; seat 0 always moves first

private fun digest(text: String): ByteArray = MessageDigest.getInstance("SHA-256").digest(text.toByteArray(Charsets.UTF_8))

/** A game where the players take turns making moves of type [M]. */
interface TurnGame<M> {
    val turn: Int
    val over: Boolean
    fun legal(): List<M>
    fun play(move: M): Boolean
}

/** Feed each player's moves to the game whenever the rules say it's their turn; nothing after an illegal move counts. */
fun <M, G : TurnGame<M>> replayTurns(game: G, first: List<M>, second: List<M>): G {
    val lists = listOf(first, second)
    val used = intArrayOf(0, 0)
    while (!game.over) {
        val seat = game.turn
        if (used[seat] >= lists[seat].size || !game.play(lists[seat][used[seat]])) break
        used[seat]++
    }
    return game
}

/** The moves of a game where the players simply alternate, in playing order. */
fun interleave(first: List<Int>, second: List<Int>): List<Int> {
    val order = ArrayList<Int>()
    for ((i, move) in first.withIndex()) {
        order.add(move)
        if (i >= second.size) break
        order.add(second[i])
    }
    return order
}

// ----- tic-tac-toe and ultimate tic-tac-toe ----------------------------------------------------------

private val LINES = listOf(listOf(0, 1, 2), listOf(3, 4, 5), listOf(6, 7, 8), listOf(0, 3, 6), listOf(1, 4, 7), listOf(2, 5, 8),
    listOf(0, 4, 8), listOf(2, 4, 6))

private fun lineOf(cells: List<String>): Pair<String, List<Int>>? {
    for (line in LINES) {
        val (a, b, c) = line
        if (cells[a] in MARKS && cells[a] == cells[b] && cells[b] == cells[c]) return cells[a] to line
    }
    return null
}

/** Tic-tac-toe, or ultimate tic-tac-toe (nine boards; your move picks the board your opponent plays next). */
class Board(val ultimate: Boolean) : TurnGame<Int> {
    val cells = MutableList(if (ultimate) 81 else 9) { "" }
    val small = MutableList(9) { "" } // ultimate: "x", "o", "draw" or ""
    override var turn = 0
    var winner: Int? = null
    var draw = false
    var line: List<Int> = emptyList()
    var last: Int? = null
    var forced: Int? = null // ultimate: the small board the next move must be in, if any

    override val over: Boolean get() = winner != null || draw

    override fun legal(): List<Int> {
        if (over) return emptyList()
        if (!ultimate) return (0 until 9).filter { cells[it].isEmpty() }
        val boards = forced?.let { listOf(it) } ?: (0 until 9).filter { small[it].isEmpty() }
        return boards.flatMap { b -> (0 until 9).map { b * 9 + it } }.filter { cells[it].isEmpty() }
    }

    override fun play(move: Int): Boolean {
        if (move !in legal()) return false
        val mark = MARKS[turn]
        cells[move] = mark
        last = move
        if (ultimate) {
            val b = move / 9
            val c = move % 9
            val boardCells = cells.subList(b * 9, b * 9 + 9)
            if (lineOf(boardCells) != null) small[b] = mark else if (boardCells.all { it.isNotEmpty() }) small[b] = "draw"
            val overall = lineOf(small)
            if (overall != null) {
                winner = turn
                line = overall.second
            } else if (small.all { it.isNotEmpty() }) {
                draw = true
            }
            forced = if (small[c].isNotEmpty()) null else c
        } else {
            val won = lineOf(cells)
            if (won != null) {
                winner = turn
                line = won.second
            } else if (cells.all { it.isNotEmpty() }) {
                draw = true
            }
        }
        turn = 1 - turn
        return true
    }
}

fun replayBoard(ultimate: Boolean, first: List<Int>, second: List<Int>): Board = replayTurns(Board(ultimate), first, second)

// ----- four in a row ------------------------------------------------------------------------------------

const val C4_ROWS = 6
const val C4_COLS = 7

/** The first four in a row on the board (scanning rows, then columns), as cell indexes. */
fun fourInARow(cells: List<String>): List<Int>? {
    for (r in 0 until C4_ROWS) {
        for (c in 0 until C4_COLS) {
            val mark = cells[r * C4_COLS + c]
            if (mark.isEmpty()) continue
            for ((dr, dc) in listOf(0 to 1, 1 to 0, 1 to 1, 1 to -1)) {
                val endR = r + 3 * dr
                val endC = c + 3 * dc
                if (endR !in 0 until C4_ROWS || endC !in 0 until C4_COLS) continue
                val run = (0 until 4).map { k -> (r + k * dr) * C4_COLS + c + k * dc }
                if (run.all { cells[it] == mark }) return run
            }
        }
    }
    return null
}

/** Drop discs into seven columns; four in a row wins. A move is a column (0-6). */
class FourInARow : TurnGame<Int> {
    val cells = MutableList(C4_ROWS * C4_COLS) { "" } // row 0 is the top
    override var turn = 0
    var winner: Int? = null
    var draw = false
    var line: List<Int> = emptyList()
    var last: Int? = null

    override val over: Boolean get() = winner != null || draw

    override fun legal(): List<Int> = if (over) emptyList() else (0 until C4_COLS).filter { cells[it].isEmpty() }

    override fun play(move: Int): Boolean {
        if (move !in legal()) return false
        val row = (0 until C4_ROWS).last { cells[it * C4_COLS + move].isEmpty() }
        val cell = row * C4_COLS + move
        cells[cell] = MARKS[turn]
        last = cell
        val found = fourInARow(cells)
        if (found != null) {
            winner = turn
            line = found
        } else if (cells.all { it.isNotEmpty() }) {
            draw = true
        }
        turn = 1 - turn
        return true
    }
}

// ----- checkers -----------------------------------------------------------------------------------------

const val CHECKERS_QUIET_LIMIT = 80 // turns in a row without a capture: a draw

private fun seatOf(piece: String): Int? = when (piece) {
    "x", "X" -> 0
    "o", "O" -> 1
    else -> null
}

private fun directions(piece: String): List<Pair<Int, Int>> = when (piece) {
    "x" -> listOf(-1 to -1, -1 to 1) // seat 0 starts at the bottom and moves up
    "o" -> listOf(1 to -1, 1 to 1)
    else -> listOf(-1 to -1, -1 to 1, 1 to -1, 1 to 1) // kings
}

private fun crowned(piece: String, row: Int) = (piece == "x" && row == 0) || (piece == "o" && row == 7)

/**
 * English draughts on the dark squares of an 8x8 board (index row * 8 + column). Jumping is
 * compulsory and must go on while it can; a move is the whole path. Reaching the far row crowns a
 * man and ends the move. You lose when you can't move.
 */
class Checkers : TurnGame<List<Int>> {
    val board = MutableList(64) { i ->
        val r = i / 8
        val c = i % 8
        if ((r + c) % 2 == 1) (if (r <= 2) "o" else if (r >= 5) "x" else "") else ""
    }
    override var turn = 0
    var winner: Int? = null
    var draw = false
    var quiet = 0
    var last: List<Int> = emptyList()
    private var cached: List<List<Int>>? = null

    override val over: Boolean get() = winner != null || draw

    /** Call after changing [board] or [turn] by hand (tests). */
    fun forget() {
        cached = null
    }

    override fun legal(): List<List<Int>> {
        if (over) return emptyList()
        return cached ?: moves().also { cached = it }
    }

    private fun moves(): List<List<Int>> {
        val jumps = ArrayList<List<Int>>()
        val steps = ArrayList<List<Int>>()
        for (square in 0 until 64) {
            val piece = board[square]
            if (seatOf(piece) != turn) continue
            jumps.addAll(jumps(square, piece))
            val r = square / 8
            val c = square % 8
            for ((dr, dc) in directions(piece)) {
                val tr = r + dr
                val tc = c + dc
                if (tr in 0 until 8 && tc in 0 until 8 && board[tr * 8 + tc].isEmpty()) steps.add(listOf(square, tr * 8 + tc))
            }
        }
        return jumps.ifEmpty { steps }
    }

    private fun jumps(start: Int, piece: String): List<List<Int>> {
        val found = ArrayList<List<Int>>()
        fun walk(square: Int, path: List<Int>, taken: Set<Int>) {
            val r = square / 8
            val c = square % 8
            var wentOn = false
            for ((dr, dc) in directions(piece)) {
                val lr = r + 2 * dr
                val lc = c + 2 * dc
                if (lr !in 0 until 8 || lc !in 0 until 8) continue
                val middle = (r + dr) * 8 + c + dc
                val landing = lr * 8 + lc
                if (middle in taken || seatOf(board[middle]) != 1 - turn) continue
                if (board[landing].isNotEmpty() && landing != start) continue
                wentOn = true
                if (crowned(piece, lr)) found.add(path + landing) else walk(landing, path + landing, taken + middle)
            }
            if (!wentOn && path.size > 1) found.add(path)
        }
        walk(start, listOf(start), emptySet())
        return found
    }

    override fun play(move: List<Int>): Boolean {
        if (move !in legal()) return false
        var piece = board[move[0]]
        board[move[0]] = ""
        var captured = false
        for (i in 0 until move.size - 1) {
            val a = move[i]
            val b = move[i + 1]
            if (kotlin.math.abs(b / 8 - a / 8) == 2) {
                board[(a + b) / 2] = ""
                captured = true
            }
        }
        if (crowned(piece, move.last() / 8)) piece = piece.uppercase()
        board[move.last()] = piece
        last = move
        quiet = if (captured) 0 else quiet + 1
        turn = 1 - turn
        cached = null
        if (legal().isEmpty()) winner = 1 - turn else if (quiet >= CHECKERS_QUIET_LIMIT) draw = true
        return true
    }
}

// ----- dots and boxes -----------------------------------------------------------------------------------

const val DOTS_SIZE = 5 // boxes per side: 6 x 6 dots
const val DOTS_LINES = 2 * DOTS_SIZE * (DOTS_SIZE + 1)
private const val HORIZONTAL_LINES = DOTS_SIZE * (DOTS_SIZE + 1)

/** Top, bottom, left and right line of a box (horizontal lines first, then vertical ones). */
fun boxLines(box: Int): List<Int> {
    val r = box / DOTS_SIZE
    val c = box % DOTS_SIZE
    return listOf(r * DOTS_SIZE + c, (r + 1) * DOTS_SIZE + c,
        HORIZONTAL_LINES + r * (DOTS_SIZE + 1) + c, HORIZONTAL_LINES + r * (DOTS_SIZE + 1) + c + 1)
}

fun boxesOf(line: Int): List<Int> {
    if (line < HORIZONTAL_LINES) {
        val r = line / DOTS_SIZE
        val c = line % DOTS_SIZE
        return listOfNotNull(if (r > 0) (r - 1) * DOTS_SIZE + c else null, if (r < DOTS_SIZE) r * DOTS_SIZE + c else null)
    }
    val r = (line - HORIZONTAL_LINES) / (DOTS_SIZE + 1)
    val c = (line - HORIZONTAL_LINES) % (DOTS_SIZE + 1)
    return listOfNotNull(if (c > 0) r * DOTS_SIZE + c - 1 else null, if (c < DOTS_SIZE) r * DOTS_SIZE + c else null)
}

/** Draw lines between dots; closing a box claims it and earns another turn. A move is a line. */
class Dots : TurnGame<Int> {
    val lines = MutableList(DOTS_LINES) { "" }
    val boxes = MutableList(DOTS_SIZE * DOTS_SIZE) { "" }
    val score = intArrayOf(0, 0)
    override var turn = 0
    var winner: Int? = null
    var draw = false
    var last: Int? = null

    override val over: Boolean get() = winner != null || draw

    override fun legal(): List<Int> = if (over) emptyList() else (0 until DOTS_LINES).filter { lines[it].isEmpty() }

    override fun play(move: Int): Boolean {
        if (move !in legal()) return false
        val mark = MARKS[turn]
        lines[move] = mark
        last = move
        var closed = false
        for (box in boxesOf(move)) {
            if (boxes[box].isEmpty() && boxLines(box).all { lines[it].isNotEmpty() }) {
                boxes[box] = mark
                score[turn]++
                closed = true
            }
        }
        if (boxes.all { it.isNotEmpty() }) {
            if (score[0] == score[1]) draw = true else winner = if (score[0] > score[1]) 0 else 1
        } else if (!closed) {
            turn = 1 - turn
        }
        return true
    }
}

// ----- memory match -------------------------------------------------------------------------------------

const val MEMORY_CARDS = 16 // a 4 x 4 grid: 8 pairs

/** Which picture (0-7) is on each card: the same shuffle on both sides, from the game's ID. */
fun memoryLayout(gameId: String): List<Int> {
    val deck = MutableList(MEMORY_CARDS) { it / 2 }
    for (i in MEMORY_CARDS - 1 downTo 1) {
        val n = ByteBuffer.wrap(digest("lightninglan-memory|$gameId|$i"), 0, 8).long
        val j = java.lang.Long.remainderUnsigned(n, (i + 1).toLong()).toInt()
        val t = deck[i]
        deck[i] = deck[j]
        deck[j] = t
    }
    return deck
}

/** Take turns turning over two cards; a pair is yours, and you go again. A move is one card. */
class Memory(val layout: List<Int>) : TurnGame<Int> {
    val matched = MutableList(MEMORY_CARDS) { "" }
    var pending: Int? = null // the first card of the current turn
    var shown: List<Int> = emptyList() // the last pair that didn't match (face up until the next card)
    val score = intArrayOf(0, 0)
    override var turn = 0
    var winner: Int? = null
    var draw = false
    var last: Int? = null

    override val over: Boolean get() = winner != null || draw

    override fun legal(): List<Int> =
        if (over) emptyList() else (0 until MEMORY_CARDS).filter { matched[it].isEmpty() && it != pending }

    override fun play(move: Int): Boolean {
        if (move !in legal()) return false
        last = move
        val first = pending
        if (first == null) {
            pending = move
            shown = emptyList()
            return true
        }
        pending = null
        if (layout[first] == layout[move]) {
            matched[first] = MARKS[turn]
            matched[move] = MARKS[turn]
            score[turn]++
            shown = emptyList()
            if (matched.all { it.isNotEmpty() }) {
                if (score[0] == score[1]) draw = true else winner = if (score[0] > score[1]) 0 else 1
            }
        } else {
            shown = listOf(first, move)
            turn = 1 - turn
        }
        return true
    }
}

// ----- rock-paper-scissors ---------------------------------------------------------------------------------

val CHOICES = listOf("rock", "paper", "scissors")
private val BEATS = mapOf("rock" to "scissors", "paper" to "rock", "scissors" to "paper")
const val MAX_ROUNDS = 99

/** The hash a rock-paper-scissors choice is locked in with, before anyone reveals. */
fun seal(gameId: String, round: Int, choice: String, salt: String): String =
    sha256Hex("lightninglan-rps|$gameId|$round|$choice|$salt".toByteArray(Charsets.UTF_8))

class RoundPlay(val picks: List<String>, val winner: Int?)

class Rounds {
    val rounds = ArrayList<RoundPlay>()
    val score = intArrayOf(0, 0)
    var winner: Int? = null
    var draw = false
    var cheat: Int? = null // the seat whose revealed choice didn't match what it locked in

    val current: Int get() = rounds.size
    val over: Boolean get() = winner != null || draw
}

fun replayRounds(gameId: String, bestOf: Int, commits: List<List<String>>, reveals: List<List<Pair<String, String>>>): Rounds {
    val result = Rounds()
    val target = bestOf / 2 + 1
    for (r in 0 until MAX_ROUNDS) {
        if (r >= minOf(commits[0].size, commits[1].size) || r >= minOf(reveals[0].size, reveals[1].size)) return result
        val picks = ArrayList<String>()
        for (seat in 0..1) {
            val (choice, salt) = reveals[seat][r]
            if (!MessageDigest.isEqual(seal(gameId, r, choice, salt).toByteArray(), commits[seat][r].toByteArray())) {
                result.cheat = seat
                result.winner = 1 - seat
                return result
            }
            picks.add(choice)
        }
        val winner = if (picks[0] == picks[1]) null else if (BEATS[picks[0]] == picks[1]) 0 else 1
        result.rounds.add(RoundPlay(picks, winner))
        if (winner != null) {
            result.score[winner]++
            if (result.score[winner] >= target) {
                result.winner = winner
                return result
            }
        }
    }
    result.draw = true
    return result
}

// ----- battleship -------------------------------------------------------------------------------------------

const val GRID = 10
val FLEET = listOf(5, 4, 3, 3, 2)
val SHIP_NAMES = listOf("carrier", "battleship", "cruiser", "submarine", "destroyer")
val FLEET_CELLS = FLEET.sum()

/** The cells of each ship ((bow, horizontal) per ship, in FLEET order), or null if the layout is invalid. */
fun shipCells(layout: List<Pair<Int, Boolean>>): List<List<Int>>? {
    if (layout.size != FLEET.size) return null
    val taken = HashSet<Int>()
    val ships = ArrayList<List<Int>>()
    for ((i, ship) in layout.withIndex()) {
        val (bow, horizontal) = ship
        val size = FLEET[i]
        if (bow !in 0 until GRID * GRID) return null
        if ((if (horizontal) bow % GRID else bow / GRID) + size > GRID) return null
        val cells = (0 until size).map { bow + it * (if (horizontal) 1 else GRID) }
        if (cells.any { it in taken }) return null
        taken.addAll(cells)
        ships.add(cells)
    }
    return ships
}

fun layoutText(layout: List<Pair<Int, Boolean>>): String = layout.joinToString(",") { (bow, h) -> "$bow${if (h) "h" else "v"}" }

fun parseLayout(text: String): List<Pair<Int, Boolean>>? {
    val layout = ArrayList<Pair<Int, Boolean>>()
    for (part in text.split(",")) {
        if (part.length < 2 || part.length > 4 || part.last() !in "hv" || !part.dropLast(1).all { it in '0'..'9' }) return null
        layout.add(part.dropLast(1).toInt() to (part.last() == 'h'))
    }
    return if (shipCells(layout) != null) layout else null
}

fun randomLayout(random: Random): List<Pair<Int, Boolean>> {
    while (true) {
        val layout = FLEET.map { random.nextInt(GRID * GRID) to random.nextBoolean() }
        if (shipCells(layout) != null) return layout
    }
}

/** The hash a hidden fleet is locked in with; revealed and checked when the game ends. */
fun sealFleet(gameId: String, text: String, salt: String): String =
    sha256Hex("lightninglan-battleship|$gameId|$text|$salt".toByteArray(Charsets.UTF_8))

/** What a fleet says to each shot fired at it: "miss", "hit", or "sunk" (the last hit on a ship). */
fun answerShots(ships: List<List<Int>>, shots: List<Int>): List<String> {
    val hits = HashSet<Int>()
    return shots.map { shot ->
        val ship = ships.firstOrNull { shot in it }
        if (ship == null) {
            "miss"
        } else {
            hits.add(shot)
            if (ship.all { it in hits }) "sunk" else "hit"
        }
    }
}

class Battle {
    var turn = 0
    var winner: Int? = null
    val hits = intArrayOf(0, 0) // hits each seat's fleet took
    val fired = intArrayOf(0, 0) // shots that count, per seat
    val over: Boolean get() = winner != null
}

/** shots[s]: what seat s fired; answers[s]: what seat s's fleet said to the other seat's shots. */
fun replayBattle(shots: List<List<Int>>, answers: List<List<String>>): Battle {
    val battle = Battle()
    val seen = listOf(HashSet<Int>(), HashSet<Int>())
    for ((i, cell) in interleave(shots[0], shots[1]).withIndex()) {
        val seat = i % 2
        val k = i / 2
        if (!seen[seat].add(cell)) break // a square can only be shot once
        battle.fired[seat]++
        val target = 1 - seat
        if (k < answers[target].size && answers[target][k] in setOf("hit", "sunk")) {
            battle.hits[target]++
            if (battle.hits[target] >= FLEET_CELLS) {
                battle.winner = seat
                break
            }
        }
    }
    battle.turn = if (battle.fired[0] == battle.fired[1]) 0 else 1
    return battle
}

// ----- typing race and math sprint ----------------------------------------------------------------------------

val SENTENCES = listOf(
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

fun sentenceFor(gameId: String): String = SENTENCES[(gameId.take(8).toLong(16) % SENTENCES.size).toInt()]

const val MATH_PROBLEMS = 10

/** The sums of a math sprint, from the game's ID: (question, answer). */
fun mathProblems(gameId: String): List<Pair<String, Int>> = (0 until MATH_PROBLEMS).map { r ->
    val h = digest("lightninglan-math|$gameId|$r").map { it.toInt() and 0xff }
    when (h[0] % 3) {
        0 -> {
            val a = 2 + h[1] % 49
            val b = 2 + h[2] % 49
            "$a + $b" to a + b
        }
        1 -> {
            val a = 10 + h[1] % 90
            val b = 1 + h[2] % a
            "$a − $b" to a - b
        }
        else -> {
            val a = 2 + h[1] % 11
            val b = 2 + h[2] % 11
            "$a × $b" to a * b
        }
    }
}

/** A typing race or a math sprint: each player times themselves; quicker wins (per sum, in a sprint). */
class Race {
    val points = intArrayOf(0, 0)
    val rounds = ArrayList<Int?>() // who won each scored round (null: a tie)
    var winner: Int? = null
    var draw = false
    val over: Boolean get() = winner != null || draw
}

fun replayRace(times: List<List<Int>>, rounds: Int): Race {
    val race = Race()
    for (r in 0 until minOf(times[0].size, times[1].size, rounds)) {
        val a = times[0][r]
        val b = times[1][r]
        val won = if (a == b) null else if (a < b) 0 else 1
        race.rounds.add(won)
        if (won != null) race.points[won]++
    }
    if (race.rounds.size == rounds) {
        if (race.points[0] == race.points[1]) race.draw = true else race.winner = if (race.points[0] > race.points[1]) 0 else 1
    }
    return race
}
