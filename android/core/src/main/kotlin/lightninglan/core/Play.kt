package lightninglan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.addJsonArray
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import okhttp3.OkHttpClient
import okhttp3.Request
import java.security.SecureRandom
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread
import kotlin.random.asKotlinRandom

/*
 * While you wait: head-to-head games between peers (lightninglan/play.py, the same records).
 *
 * Every peer publishes only what it did itself, at GET /api/games: the games it offers, its answer
 * to invitations, its own moves, and what each game needs besides (sealed rock-paper-scissors
 * choices, a sealed battleship fleet and its answers to shots, race times). Each side polls the
 * other and replays both halves with the same rules (Games.kt). Nothing is ever sent to another
 * peer, and a peer can only play its own side.
 */

private const val TAG = "play"
const val TICTACTOE = "tictactoe"
const val ULTIMATE = "ultimate"
const val CONNECT4 = "connect4"
const val CHECKERS = "checkers"
const val DOTS = "dots"
const val BATTLESHIP = "battleship"
const val MEMORY = "memory"
const val RPS = "rps"
const val TYPING = "typing"
const val MATH = "math"
val GAME_TITLES = linkedMapOf(
    TICTACTOE to "Tic-tac-toe",
    ULTIMATE to "Ultimate tic-tac-toe",
    CONNECT4 to "Four in a row",
    CHECKERS to "Checkers",
    DOTS to "Dots and boxes",
    BATTLESHIP to "Battleship",
    MEMORY to "Memory match",
    RPS to "Rock-paper-scissors",
    TYPING to "Typing race",
    MATH to "Math sprint",
)
private val TURN_KINDS = setOf(TICTACTOE, ULTIMATE, CONNECT4, CHECKERS, DOTS, MEMORY)
private val ORIGINAL_KINDS = setOf(TICTACTOE, ULTIMATE, RPS) // what peers that don't list their games have
// The most moves one player can make in each, and how many different moves there are.
private val MOVE_LIMITS = mapOf(TICTACTOE to (5 to 9), ULTIMATE to (41 to 81), CONNECT4 to (21 to 7), DOTS to (60 to 60),
    MEMORY to (400 to 16))
private const val CHECKERS_LIMIT = 600
val BEST_OF = listOf(1, 3, 5, 7)

private const val OFFERED = "offered"
private const val ACCEPTED = "accepted"
private const val DECLINED_STANCE = "declined"
private const val CANCELED_STANCE = "canceled"
private const val RESIGNED = "resigned"
private val STANCES = setOf(OFFERED, ACCEPTED, DECLINED_STANCE, CANCELED_STANCE, RESIGNED)

/** A game's status from one player's side. */
const val GAME_WAITING = "waiting"
const val GAME_PLAYING = "playing"
const val GAME_OVER = "over"
const val GAME_DECLINED = "declined"
const val GAME_CANCELED = "canceled"

private const val INVITE_TTL = 600_000L
private const val KEEP_CLOSED = 900_000L
private const val MAX_GAMES = 40
private const val ACTIVE_POLL = 500L
private const val MAX_PAYLOAD = 2L * 1024 * 1024
private val TYPING_TIMES = 300..600_000
private val MATH_TIMES = 100..600_000
private val ANSWERS = setOf("miss", "hit", "sunk")
private val ID_RE = Regex("^[0-9a-f]{32}$")
private val HASH_RE = Regex("^[0-9a-f]{64}$")

class PlayError(message: String, val status: Int = 409) : Exception(message)

// ----- what each peer publishes ----------------------------------------------------------------------------

/** One peer's half of a game, as it publishes it. */
data class GameRecord(
    val id: String,
    val kind: String,
    val players: List<String>, // seat 0 moves first
    val names: List<String>,
    val host: String,
    val bestOf: Int,
    var stance: String,
    val moves: MutableList<Int> = ArrayList(), // turn games: this peer's moves
    val paths: MutableList<List<Int>> = ArrayList(), // checkers: this peer's moves
    val commits: MutableList<String> = ArrayList(), // rock-paper-scissors: sealed choices
    val reveals: MutableList<Pair<String, String>> = ArrayList(), // (choice, salt) once both have locked in
    var fleet: String = "", // battleship: the sealed fleet ("" while placing ships)
    val shots: MutableList<Int> = ArrayList(), // battleship: squares fired at
    var answers: List<String> = emptyList(), // battleship: this fleet's answers to the other's shots
    var reveal: Pair<String, String>? = null, // battleship: (layout, salt), once the battle is over
    val times: MutableList<Int> = ArrayList(), // typing race (one) or math sprint (one per sum), in ms
) {
    fun toJson(): JsonObject = buildJsonObject {
        put("id", id)
        put("kind", kind)
        putJsonArray("players") { players.forEach { add(it) } }
        putJsonArray("names") { names.forEach { add(it) } }
        put("host", host)
        put("best_of", bestOf)
        put("stance", stance)
        when (kind) {
            RPS -> {
                putJsonArray("commits") { commits.forEach { add(it) } }
                putJsonArray("reveals") { reveals.forEach { (c, s) -> addJsonArray { add(c); add(s) } } }
            }
            BATTLESHIP -> {
                put("fleet", fleet)
                putJsonArray("shots") { shots.forEach { add(it) } }
                putJsonArray("answers") { answers.forEach { add(it) } }
                val r = reveal
                put("reveal", if (r == null) JsonNull else buildJsonArray { add(r.first); add(r.second) })
            }
            TYPING, MATH -> putJsonArray("times") { times.forEach { add(it) } }
            CHECKERS -> putJsonArray("moves") { paths.forEach { p -> addJsonArray { p.forEach { add(it) } } } }
            else -> putJsonArray("moves") { moves.forEach { add(it) } }
        }
    }

    fun sameGame(other: GameRecord): Boolean =
        id == other.id && kind == other.kind && players == other.players && host == other.host && bestOf == other.bestOf
}

private fun JsonElement?.str(): String? = (this as? JsonPrimitive)?.takeIf { it.isString }?.content

private fun JsonElement?.intValue(): Int? =
    (this as? JsonPrimitive)?.takeIf { !it.isString && it.content != "true" && it.content != "false" && '.' !in it.content }?.intOrNull

private fun intList(value: JsonElement?, limit: Int, range: IntRange): List<Int>? {
    val array = (value ?: JsonArray(emptyList())) as? JsonArray ?: return null
    if (array.size > limit) return null
    return array.map { e -> e.intValue()?.takeIf { it in range } ?: return null }
}

/** Validate another peer's record of a game. Null if anything about it is off. */
fun parseGameRecord(data: JsonElement): GameRecord? {
    if (data !is JsonObject) return null
    val id = data["id"].str() ?: return null
    val kind = data["kind"].str() ?: return null
    val host = data["host"].str() ?: return null
    val stance = data["stance"].str() ?: return null
    if (!ID_RE.matches(id) || kind !in GAME_TITLES || stance !in STANCES) return null
    val players = (data["players"] as? JsonArray)?.map { it.str() ?: return null } ?: return null
    if (players.size != 2 || players.any { !ID_RE.matches(it) } || players[0] == players[1] || host !in players) return null
    val rawNames = (data["names"] as? JsonArray)?.map { it.str() }
    val names = players.mapIndexed { i, p ->
        val n = rawNames?.takeIf { it.size == 2 }?.get(i)
        n?.let { displayName(it).trim().take(64) }?.ifEmpty { null } ?: p.take(8)
    }
    val bestOf = if (kind == RPS) data["best_of"].intValue()?.takeIf { it in BEST_OF } ?: return null else 1
    val record = GameRecord(id, kind, players, names, host, bestOf, stance)
    when (kind) {
        RPS -> {
            val commits = (data["commits"] ?: JsonArray(emptyList())) as? JsonArray ?: return null
            val reveals = (data["reveals"] ?: JsonArray(emptyList())) as? JsonArray ?: return null
            if (commits.size > MAX_ROUNDS || reveals.size > MAX_ROUNDS) return null
            for (c in commits) record.commits.add(c.str()?.takeIf { HASH_RE.matches(it) } ?: return null)
            for (r in reveals) {
                val pair = r as? JsonArray ?: return null
                if (pair.size != 2) return null
                val choice = pair[0].str()?.takeIf { it in CHOICES } ?: return null
                val salt = pair[1].str()?.takeIf { ID_RE.matches(it) } ?: return null
                record.reveals.add(choice to salt)
            }
        }
        BATTLESHIP -> {
            val fleet = data["fleet"]?.let { it.str() ?: return null } ?: ""
            if (fleet.isNotEmpty() && !HASH_RE.matches(fleet)) return null
            record.fleet = fleet
            record.shots.addAll(intList(data["shots"], 100, 0 until 100) ?: return null)
            val answers = (data["answers"] ?: JsonArray(emptyList())) as? JsonArray ?: return null
            if (answers.size > 100) return null
            record.answers = answers.map { a -> a.str()?.takeIf { it in ANSWERS } ?: return null }
            val reveal = data["reveal"]
            if (reveal != null && reveal !is JsonNull) {
                val pair = reveal as? JsonArray ?: return null
                if (pair.size != 2) return null
                val text = pair[0].str()?.takeIf { parseLayout(it) != null } ?: return null
                val salt = pair[1].str()?.takeIf { ID_RE.matches(it) } ?: return null
                record.reveal = text to salt
            }
        }
        TYPING, MATH -> {
            val range = if (kind == TYPING) TYPING_TIMES else MATH_TIMES
            record.times.addAll(intList(data["times"], if (kind == TYPING) 1 else MATH_PROBLEMS, range) ?: return null)
        }
        CHECKERS -> {
            val moves = (data["moves"] ?: JsonArray(emptyList())) as? JsonArray ?: return null
            if (moves.size > CHECKERS_LIMIT) return null
            for (m in moves) {
                val path = intList(m, 13, 0 until 64) ?: return null
                if (path.size < 2) return null
                record.paths.add(path)
            }
        }
        else -> {
            val (limit, below) = MOVE_LIMITS.getValue(kind)
            record.moves.addAll(intList(data["moves"], limit, 0 until below) ?: return null)
        }
    }
    return record
}

// ----- what the screens show ----------------------------------------------------------------------------------

data class Opponent(val peerId: String, val name: String, val online: Boolean = true)

data class InviteView(val id: String, val kind: String, val title: String, val bestOf: Int, val from: Opponent, val youGoFirst: Boolean)

data class RoundView(val mine: String, val theirs: String, val result: String)

data class RpsView(
    val rounds: List<RoundView>,
    val score: Pair<Int, Int>,
    val target: Int,
    val locked: Boolean,
    val choice: String?,
    val theyLocked: Boolean,
)

data class Shot(val cell: Int, val result: String?)

data class BattleView(
    val phase: String, // "setup", "battle" or "over"
    val ready: Boolean,
    val theyReady: Boolean,
    val ships: List<List<Int>>, // mine
    val incoming: List<Shot>, // their shots at my fleet
    val outgoing: List<Shot>, // my shots at theirs
    val theirShips: List<List<Int>>, // once they've revealed them
    val sunk: Pair<Int, Int>, // ships I sank, ships I lost
)

data class RaceView(val sentence: String, val myTime: Int?, val theyDone: Boolean, val theirTime: Int?)

data class SprintView(
    val problems: List<Pair<String, Int>>,
    val myTimes: List<Int>,
    val theirCount: Int,
    val theirTimes: List<Int>, // only the sums you've done too
    val score: Pair<Int, Int>,
)

data class GameView(
    val id: String,
    val kind: String,
    val title: String,
    val bestOf: Int,
    val host: Boolean,
    val seat: Int,
    val mark: String,
    val opponent: Opponent,
    val status: String,
    val result: String?,
    val reason: String,
    val yourTurn: Boolean,
    val created: Long,
    val board: List<String> = emptyList(), // tic-tac-toe, ultimate, four in a row, checkers
    val line: List<Int> = emptyList(),
    val last: Int? = null,
    val allowed: List<Int> = emptyList(),
    val small: List<String> = emptyList(),
    val forced: Int? = null,
    val paths: List<List<Int>> = emptyList(), // checkers: legal moves
    val lastPath: List<Int> = emptyList(),
    val lines: List<String> = emptyList(), // dots and boxes
    val boxes: List<String> = emptyList(),
    val cards: List<Int?> = emptyList(), // memory: the picture on each card, if it shows
    val matched: List<String> = emptyList(),
    val pending: Int? = null,
    val shown: List<Int> = emptyList(),
    val score: Pair<Int, Int>? = null, // checkers (pieces), dots and boxes, memory: mine, theirs
    val rps: RpsView? = null,
    val battle: BattleView? = null,
    val race: RaceView? = null,
    val sprint: SprintView? = null,
)

data class PlayView(val games: List<GameView>, val invites: List<InviteView>, val opponents: List<Opponent>)

// ----- this peer's games --------------------------------------------------------------------------------------

private class Game(val mine: GameRecord, var theirs: GameRecord? = null) {
    val secrets = HashMap<Int, Pair<String, String>>() // rock-paper-scissors: (choice, salt) until revealed
    val choices = HashMap<Int, String>() // rock-paper-scissors: what I picked each round
    var fleet: List<Pair<Int, Boolean>>? = null // battleship: my ships (never published until the end)
    var fleetSalt = ""
    val created = System.currentTimeMillis()
    var closedAt: Long? = null

    val id: String get() = mine.id

    fun seatOf(peerId: String): Int = mine.players.indexOf(peerId)
}

private class Outcome(
    val status: String,
    val result: String? = null,
    val reason: String = "",
    val engine: TurnGame<*>? = null,
    val rounds: Rounds? = null,
    val battle: Battle? = null,
    val race: Race? = null,
)

private fun newEngine(kind: String, gameId: String): TurnGame<*> = when (kind) {
    TICTACTOE -> Board(false)
    ULTIMATE -> Board(true)
    CONNECT4 -> FourInARow()
    CHECKERS -> Checkers()
    DOTS -> Dots()
    else -> Memory(memoryLayout(gameId))
}

@Suppress("UNCHECKED_CAST")
private fun replayEngine(kind: String, gameId: String, first: GameRecord?, second: GameRecord?): TurnGame<*> =
    if (kind == CHECKERS) {
        replayTurns(Checkers(), first?.paths ?: emptyList(), second?.paths ?: emptyList())
    } else {
        replayTurns(newEngine(kind, gameId) as TurnGame<Int>, first?.moves ?: emptyList(), second?.moves ?: emptyList())
    }

/** Whether a revealed fleet is the one that was sealed, and told the truth about every shot. */
private fun fleetOk(gameId: String, record: GameRecord, shotsAtIt: List<Int>): Boolean {
    val (text, salt) = record.reveal ?: return false
    if (record.fleet.isEmpty()) return false
    val layout = parseLayout(text) ?: return false
    if (sealFleet(gameId, text, salt) != record.fleet) return false
    val truth = answerShots(shipCells(layout)!!, shotsAtIt.take(record.answers.size))
    return truth == record.answers.take(truth.size)
}

class Arcade(
    private val me: String,
    private val ownName: () -> String,
    private val auth: Authenticator,
    private val peers: PeerTable,
    private val refreshMs: Long,
    client: OkHttpClient,
) {
    private val client = client.newBuilder().readTimeout(5, TimeUnit.SECONDS).build()
    private val games = LinkedHashMap<String, Game>()
    private val invites = LinkedHashMap<String, GameRecord>()
    private val lock = Any()
    private val random = SecureRandom()
    private val json = Json { ignoreUnknownKeys = true }

    /** Bumps on every change. */
    @Volatile
    var version = 0
        private set

    @Volatile
    private var running = false
    private var loop: Thread? = null
    private val pollers = Executors.newCachedThreadPool { r -> Thread(r, "lightninglan-play").apply { isDaemon = true } }
    private val polled = ConcurrentHashMap<String, Long>()
    private val etags = ConcurrentHashMap<String, String>()
    private val inflight: MutableSet<String> = ConcurrentHashMap.newKeySet()

    @Volatile
    private var publishedCache: Triple<Int, ByteArray, String>? = null

    private fun changed() {
        version++
    }

    // ----- lifecycle -----------------------------------------------------------------------------

    @Synchronized
    fun start() {
        if (running) return
        running = true
        loop = thread(name = "lightninglan-play-loop", isDaemon = true) { run() }
    }

    @Synchronized
    fun stop() {
        running = false
        loop?.join(2_000)
        loop = null
        polled.clear()
        etags.clear()
    }

    // ----- what other peers read (GET /api/games) --------------------------------------------------

    fun published(): Pair<ByteArray, String> = synchronized(lock) {
        publishedCache?.let { if (it.first == version) return it.second to it.third }
        val body = buildJsonObject {
            put("peer_id", me)
            put("games", buildJsonArray { for (g in games.values) add(g.mine.toJson()) })
        }.toString().toByteArray(Charsets.UTF_8)
        val etag = "\"" + sha256Hex(body).take(32) + "\""
        publishedCache = Triple(version, body, etag)
        body to etag
    }

    // ----- the outcome of a game, seen from here -----------------------------------------------------

    private fun opponentId(g: Game): String = if (g.mine.players[0] == me) g.mine.players[1] else g.mine.players[0]

    private fun opponentName(g: Game): String {
        val pid = opponentId(g)
        return peers.get(pid)?.name?.takeIf { it.isNotEmpty() } ?: g.mine.names[g.seatOf(pid)]
    }

    /** Both players' records, by seat. */
    private fun halves(g: Game): Pair<GameRecord?, GameRecord?> =
        if (g.seatOf(me) == 0) g.mine to g.theirs else g.theirs to g.mine

    private fun outcome(g: Game): Outcome {
        val mine = g.mine
        val theirs = g.theirs
        val hostIsMe = mine.host == me
        val theirStance = theirs?.stance
        val hostStance = if (hostIsMe) mine.stance else theirStance ?: OFFERED
        val guestStance = if (hostIsMe) theirStance else mine.stance
        val opponent = opponentName(g)
        if (guestStance !in setOf(ACCEPTED, RESIGNED, DECLINED_STANCE)) {
            if (hostStance == CANCELED_STANCE) return Outcome(GAME_CANCELED, reason = "Invitation withdrawn")
            return Outcome(GAME_WAITING)
        }
        if (guestStance == DECLINED_STANCE) {
            return Outcome(GAME_DECLINED, reason = if (hostIsMe) "$opponent said no thanks" else "You declined")
        }
        val mySeat = g.seatOf(me)
        // The host withdrawing just as the guest accepted counts as resigning, on both sides.
        val iGaveUp = mine.stance == RESIGNED || (hostIsMe && mine.stance == CANCELED_STANCE)
        val theyGaveUp = theirStance == RESIGNED || (!hostIsMe && theirStance == CANCELED_STANCE)
        if (iGaveUp && theyGaveUp) return Outcome(GAME_OVER, "draw", "You both gave up")
        if (iGaveUp) return Outcome(GAME_OVER, "lost", "You resigned")
        if (theyGaveUp) return Outcome(GAME_OVER, "won", "$opponent resigned")
        val (first, second) = halves(g)

        fun finished(winner: Int?, draw: Boolean, why: String, engine: TurnGame<*>? = null, rounds: Rounds? = null,
                     battle: Battle? = null, race: Race? = null): Outcome = when {
            draw -> Outcome(GAME_OVER, "draw", why, engine, rounds, battle, race)
            winner == null -> Outcome(GAME_PLAYING, engine = engine, rounds = rounds, battle = battle, race = race)
            else -> Outcome(GAME_OVER, if (winner == mySeat) "won" else "lost", why, engine, rounds, battle, race)
        }

        return when (val kind = mine.kind) {
            RPS -> {
                val rounds = replayRounds(mine.id, mine.bestOf, listOf(first?.commits ?: emptyList(), second?.commits ?: emptyList()),
                    listOf(first?.reveals ?: emptyList(), second?.reveals ?: emptyList()))
                when (rounds.cheat) {
                    null -> finished(rounds.winner, rounds.draw, if (rounds.draw) "$MAX_ROUNDS rounds without a winner" else "", rounds = rounds)
                    mySeat -> Outcome(GAME_OVER, "lost", "Your choice didn't match what you locked in", rounds = rounds)
                    else -> Outcome(GAME_OVER, "won", "$opponent's choice didn't match what they locked in", rounds = rounds)
                }
            }
            BATTLESHIP -> {
                val bothReady = first != null && first.fleet.isNotEmpty() && second != null && second.fleet.isNotEmpty()
                val battle = if (bothReady) {
                    replayBattle(listOf(first!!.shots, second!!.shots), listOf(first.answers, second.answers))
                } else {
                    null
                }
                when {
                    theirs?.reveal != null && !fleetOk(mine.id, theirs, mine.shots) ->
                        Outcome(GAME_OVER, "won", "$opponent's fleet didn't match their answers", battle = battle)
                    battle == null -> Outcome(GAME_PLAYING)
                    else -> finished(battle.winner, false, "Fleet sunk", battle = battle)
                }
            }
            TYPING, MATH -> {
                val race = replayRace(listOf(first?.times ?: emptyList(), second?.times ?: emptyList()), if (kind == TYPING) 1 else MATH_PROBLEMS)
                val why = if (kind == TYPING) "Quicker" else "${race.points[mySeat]} sums to ${race.points[1 - mySeat]}"
                finished(race.winner, race.draw, why, race = race)
            }
            else -> {
                val engine = replayEngine(kind, mine.id, first, second)
                val (winner, draw) = when (engine) {
                    is Board -> engine.winner to engine.draw
                    is FourInARow -> engine.winner to engine.draw
                    is Checkers -> engine.winner to engine.draw
                    is Dots -> engine.winner to engine.draw
                    is Memory -> engine.winner to engine.draw
                    else -> null to false
                }
                val why = when (engine) {
                    is Dots -> "${engine.score[mySeat]} boxes to ${engine.score[1 - mySeat]}"
                    is Memory -> "${engine.score[mySeat]} pairs to ${engine.score[1 - mySeat]}"
                    is Checkers -> if (draw) "40 moves each without a capture" else "No moves left"
                    is FourInARow -> if (winner != null) "Four in a row" else "The board is full"
                    else -> if (winner != null) "Three in a row" else "No more moves"
                }
                finished(winner, draw, why, engine = engine)
            }
        }
    }

    // ----- what the UI shows ---------------------------------------------------------------------------

    fun view(): PlayView {
        val (gameViews, inviteViews) = synchronized(lock) {
            games.values.filter { it.closedAt == null }.sortedByDescending { it.created }.map(::gameView) to
                invites.values.map(::inviteView)
        }
        val opponents = peers.online().filter { it.play }.map { Opponent(it.peerId, it.name) }
        return PlayView(gameViews, inviteViews, opponents)
    }

    private fun inviteView(r: GameRecord): InviteView {
        val name = peers.get(r.host)?.name ?: r.names[r.players.indexOf(r.host)]
        return InviteView(r.id, r.kind, GAME_TITLES.getValue(r.kind), r.bestOf, Opponent(r.host, name), r.players[0] == me)
    }

    private fun gameView(g: Game): GameView {
        val out = outcome(g)
        val mySeat = g.seatOf(me)
        val them = 1 - mySeat
        val opponentId = opponentId(g)
        val opponent = Opponent(opponentId, opponentName(g), peers.get(opponentId)?.online == true)
        val playing = out.status == GAME_PLAYING
        val kind = g.mine.kind
        val base = GameView(
            g.id, kind, GAME_TITLES.getValue(kind), g.mine.bestOf, g.mine.host == me, mySeat, MARKS[mySeat], opponent,
            out.status, out.result, out.reason, false, g.created,
        )
        return when (kind) {
            RPS -> {
                val rounds = out.rounds ?: Rounds()
                val current = rounds.current
                val locked = g.mine.commits.size > current
                val theirCommits = g.theirs?.commits?.size ?: 0
                base.copy(
                    yourTurn = playing && !locked,
                    rps = RpsView(
                        rounds.rounds.map {
                            RoundView(it.picks[mySeat], it.picks[them], when (it.winner) {
                                null -> "draw"
                                mySeat -> "won"
                                else -> "lost"
                            })
                        },
                        rounds.score[mySeat] to rounds.score[them],
                        g.mine.bestOf / 2 + 1,
                        locked && playing,
                        if (locked && playing) g.choices[current] else null,
                        playing && theirCommits > current,
                    ),
                )
            }
            BATTLESHIP -> battleView(g, out, base, mySeat)
            TYPING, MATH -> raceView(g, out, base)
            else -> {
                val engine = out.engine ?: replayEngine(kind, g.id, null, null)
                val yourTurn = playing && engine.turn == mySeat
                when (engine) {
                    is Board -> base.copy(
                        yourTurn = yourTurn, board = engine.cells.toList(), line = engine.line, last = engine.last,
                        allowed = if (yourTurn) engine.legal() else emptyList(),
                        small = if (engine.ultimate) engine.small.toList() else emptyList(), forced = engine.forced,
                    )
                    is FourInARow -> base.copy(
                        yourTurn = yourTurn, board = engine.cells.toList(), line = engine.line, last = engine.last,
                        allowed = if (yourTurn) engine.legal() else emptyList(),
                    )
                    is Checkers -> {
                        val pieces = MARKS.map { m -> engine.board.count { it.lowercase() == m } }
                        base.copy(
                            yourTurn = yourTurn, board = engine.board.toList(), lastPath = engine.last,
                            paths = if (yourTurn) engine.legal() else emptyList(), score = pieces[mySeat] to pieces[them],
                        )
                    }
                    is Dots -> base.copy(
                        yourTurn = yourTurn, lines = engine.lines.toList(), boxes = engine.boxes.toList(), last = engine.last,
                        allowed = if (yourTurn) engine.legal() else emptyList(), score = engine.score[mySeat] to engine.score[them],
                    )
                    is Memory -> {
                        val visible = HashSet<Int>()
                        engine.matched.forEachIndexed { i, m -> if (m.isNotEmpty()) visible.add(i) }
                        visible.addAll(engine.shown)
                        engine.pending?.let { visible.add(it) }
                        if (out.status == GAME_OVER) visible.addAll(0 until MEMORY_CARDS)
                        base.copy(
                            yourTurn = yourTurn, cards = engine.layout.mapIndexed { i, v -> if (i in visible) v else null },
                            matched = engine.matched.toList(), pending = engine.pending, shown = engine.shown, last = engine.last,
                            allowed = if (yourTurn) engine.legal() else emptyList(), score = engine.score[mySeat] to engine.score[them],
                        )
                    }
                    else -> base
                }
            }
        }
    }

    private fun battleView(g: Game, out: Outcome, base: GameView, mySeat: Int): GameView {
        val theirs = g.theirs
        val battle = out.battle
        val ready = g.mine.fleet.isNotEmpty()
        val theyReady = theirs != null && theirs.fleet.isNotEmpty()
        val phase = if (!(ready && theyReady)) "setup" else if (out.status == GAME_OVER) "over" else "battle"
        val theirShots = theirs?.shots ?: emptyList()
        val incoming = if (battle == null) emptyList() else theirShots.take(battle.fired[1 - mySeat]).mapIndexed { k, c ->
            Shot(c, g.mine.answers.getOrNull(k))
        }
        val outgoing = if (battle == null) emptyList() else g.mine.shots.take(battle.fired[mySeat]).mapIndexed { k, c ->
            Shot(c, theirs?.answers?.getOrNull(k))
        }
        val theirShips = theirs?.reveal?.let { (text, _) -> parseLayout(text)?.let(::shipCells) } ?: emptyList()
        return base.copy(
            yourTurn = out.status == GAME_PLAYING && phase == "battle" && battle != null && battle.turn == mySeat,
            battle = BattleView(
                phase, ready, theyReady, g.fleet?.let { shipCells(it) } ?: emptyList(), incoming, outgoing, theirShips,
                outgoing.count { it.result == "sunk" } to incoming.count { it.result == "sunk" },
            ),
        )
    }

    private fun raceView(g: Game, out: Outcome, base: GameView): GameView {
        val mine = g.mine.times
        val theirs = g.theirs?.times ?: emptyList()
        val playing = out.status == GAME_PLAYING
        if (g.mine.kind == TYPING) {
            return base.copy(
                yourTurn = playing && mine.isEmpty(),
                race = RaceView(sentenceFor(g.id), mine.firstOrNull(), theirs.isNotEmpty(), if (mine.isNotEmpty()) theirs.firstOrNull() else null),
            )
        }
        val race = out.race ?: Race()
        val mySeat = g.seatOf(me)
        return base.copy(
            yourTurn = playing && mine.size < MATH_PROBLEMS,
            sprint = SprintView(mathProblems(g.id), mine.toList(), theirs.size, theirs.take(mine.size),
                race.points[mySeat] to race.points[1 - mySeat]),
        )
    }

    // ----- what the local player does --------------------------------------------------------------------

    private fun get(gameId: String): Game {
        val g = games[gameId]
        if (g == null || g.closedAt != null) throw PlayError("No such game.", 404)
        return g
    }

    private fun randomHex(bytes: Int): String = ByteArray(bytes).also { random.nextBytes(it) }.toHex()

    /** Challenge a player. Who moves first: [first] if given, else see [goesFirstAgainst]. */
    fun invite(peerId: String, kind: String, bestOf: Int = 3, first: Boolean? = null): GameView {
        if (kind !in GAME_TITLES) throw PlayError("Unknown game.", 400)
        if (kind == RPS && bestOf !in BEST_OF) throw PlayError("Best of 1, 3, 5 or 7.", 400)
        val peer = peers.get(peerId)
        if (peer == null || !peer.online) throw PlayError("That player isn't online.", 404)
        if (!peer.play) throw PlayError("${peer.name} has an older LightningLAN without games.", 409)
        if (kind !in (peer.games ?: ORIGINAL_KINDS)) throw PlayError("${peer.name}'s LightningLAN doesn't have ${GAME_TITLES[kind]} yet.", 409)
        synchronized(lock) {
            val meFirst = first ?: goesFirstAgainst(peerId)
            makeRoom()
            val players = if (meFirst) listOf(me, peerId) else listOf(peerId, me)
            val names = if (meFirst) listOf(ownName(), peer.name) else listOf(peer.name, ownName())
            val record = GameRecord(randomHex(16), kind, players, names, me, if (kind == RPS) bestOf else 1, OFFERED)
            val g = Game(record)
            games[record.id] = g
            changed()
            CoreLog.i(TAG, "invited ${peer.name} to ${GAME_TITLES[kind]}")
            return gameView(g)
        }
    }

    /** Back-to-back games against the same player take turns going first; otherwise it's random. */
    private fun goesFirstAgainst(peerId: String): Boolean {
        val last = games.values // in the order they were made, so a tie on the clock goes to the later one
            .filter { peerId in it.mine.players && outcome(it).status in setOf(GAME_PLAYING, GAME_OVER) }
            .sortedBy { it.created }.lastOrNull() ?: return random.nextBoolean()
        return last.mine.players[0] != me
    }

    private fun makeRoom() {
        if (games.size < MAX_GAMES) return
        val finished = games.values.filter { it.closedAt != null || outcome(it).status != GAME_PLAYING }.minByOrNull { it.created }
            ?: throw PlayError("Too many games at once: finish or close some first.")
        games.remove(finished.id)
    }

    /** Accept (or decline) an invitation. Returns the game when accepted. */
    fun answer(gameId: String, accept: Boolean): GameView? = synchronized(lock) {
        val invite = invites.remove(gameId) ?: throw PlayError("That invitation isn't open any more.", 404)
        makeRoom()
        val mine = GameRecord(invite.id, invite.kind, invite.players, invite.names, invite.host, invite.bestOf,
            if (accept) ACCEPTED else DECLINED_STANCE)
        val g = Game(mine, invite)
        if (!accept) g.closedAt = System.currentTimeMillis() // published so the host hears the answer
        games[gameId] = g
        changed()
        if (accept) gameView(g) else null
    }

    fun withdraw(gameId: String) = synchronized(lock) {
        val g = get(gameId)
        if (g.mine.host != me || outcome(g).status != GAME_WAITING) throw PlayError("Only an unanswered invitation can be withdrawn.")
        g.mine.stance = CANCELED_STANCE
        g.closedAt = System.currentTimeMillis()
        changed()
    }

    fun resign(gameId: String): GameView = synchronized(lock) {
        val g = get(gameId)
        if (outcome(g).status != GAME_PLAYING) throw PlayError("That game isn't being played.")
        g.mine.stance = RESIGNED
        changed()
        gameView(g)
    }

    fun close(gameId: String) = synchronized(lock) {
        val g = get(gameId)
        val status = outcome(g).status
        if (status == GAME_PLAYING) throw PlayError("Resign first, or finish the game.")
        if (status == GAME_WAITING) g.mine.stance = CANCELED_STANCE
        g.closedAt = System.currentTimeMillis()
        changed()
    }

    private fun playing(gameId: String, kinds: Set<String>): Pair<Game, Outcome> {
        val g = get(gameId)
        if (g.mine.kind !in kinds) throw PlayError("That's not a move in this game.", 400)
        val out = outcome(g)
        if (out.status != GAME_PLAYING) throw PlayError("That game isn't being played.")
        return g to out
    }

    /** A turn in tic-tac-toe, ultimate, four in a row (a column), dots and boxes (a line) or memory match (a card). */
    fun move(gameId: String, move: Int): GameView = synchronized(lock) {
        val (g, out) = playing(gameId, TURN_KINDS - CHECKERS)
        @Suppress("UNCHECKED_CAST")
        val engine = out.engine as TurnGame<Int>
        if (engine.turn != g.seatOf(me)) throw PlayError("It's not your turn.")
        if (move !in engine.legal()) throw PlayError("You can't play there.", 400)
        g.mine.moves.add(move)
        changed()
        gameView(g)
    }

    /** A checkers move: the whole path of squares. */
    fun movePath(gameId: String, path: List<Int>): GameView = synchronized(lock) {
        val (g, out) = playing(gameId, setOf(CHECKERS))
        val engine = out.engine as Checkers
        if (engine.turn != g.seatOf(me)) throw PlayError("It's not your turn.")
        if (path !in engine.legal()) throw PlayError("You can't move there.", 400)
        g.mine.paths.add(path.toList())
        changed()
        gameView(g)
    }

    fun choose(gameId: String, choice: String): GameView = synchronized(lock) {
        val (g, out) = playing(gameId, setOf(RPS))
        if (choice !in CHOICES) throw PlayError("Choose rock, paper or scissors.", 400)
        val current = out.rounds?.current ?: 0
        if (g.mine.commits.size != current) throw PlayError("You've already locked in this round.")
        val salt = randomHex(16)
        g.secrets[current] = choice to salt
        g.choices[current] = choice
        g.mine.commits.add(seal(g.id, current, choice, salt))
        respond(g)
        changed()
        gameView(g)
    }

    fun shuffleFleet(gameId: String): GameView = synchronized(lock) {
        val (g, _) = playing(gameId, setOf(BATTLESHIP))
        if (g.mine.fleet.isNotEmpty()) throw PlayError("Your ships are already in place.")
        g.fleet = randomLayout(random.asKotlinRandom())
        changed()
        gameView(g)
    }

    fun fleetReady(gameId: String): GameView = synchronized(lock) {
        val (g, _) = playing(gameId, setOf(BATTLESHIP))
        if (g.mine.fleet.isNotEmpty()) throw PlayError("Your ships are already in place.")
        val layout = g.fleet ?: randomLayout(random.asKotlinRandom()).also { g.fleet = it }
        g.fleetSalt = randomHex(16)
        g.mine.fleet = sealFleet(g.id, layoutText(layout), g.fleetSalt)
        respond(g)
        changed()
        gameView(g)
    }

    fun fire(gameId: String, cell: Int): GameView = synchronized(lock) {
        val (g, out) = playing(gameId, setOf(BATTLESHIP))
        val battle = out.battle ?: throw PlayError("Both fleets have to be in place first.")
        if (battle.turn != g.seatOf(me)) throw PlayError("It's not your turn.")
        if (cell !in 0 until 100 || cell in g.mine.shots) throw PlayError("Pick a square you haven't fired at.", 400)
        g.mine.shots.add(cell)
        changed()
        gameView(g)
    }

    /** Typing race: the time for the sentence. Math sprint: the time for the next sum. */
    fun finish(gameId: String, timeMs: Int): GameView = synchronized(lock) {
        val (g, _) = playing(gameId, setOf(TYPING, MATH))
        val range = if (g.mine.kind == TYPING) TYPING_TIMES else MATH_TIMES
        if (timeMs !in range) throw PlayError("That time doesn't look right.", 400)
        if (g.mine.times.size >= (if (g.mine.kind == TYPING) 1 else MATH_PROBLEMS)) throw PlayError("You've already finished.")
        g.mine.times.add(timeMs)
        changed()
        gameView(g)
    }

    fun rematch(gameId: String): GameView {
        val previous = synchronized(lock) {
            val g = get(gameId)
            if (outcome(g).status !in setOf(GAME_OVER, GAME_DECLINED, GAME_CANCELED)) throw PlayError("Finish this game first.")
            g.closedAt = System.currentTimeMillis()
            changed()
            g.mine
        }
        val opponent = if (previous.players[0] == me) previous.players[1] else previous.players[0]
        return invite(opponent, previous.kind, previous.bestOf, first = previous.players[0] != me) // take turns going first
    }

    /** What this side publishes by itself once the other side has done something. */
    private fun respond(g: Game) {
        val theirs = g.theirs ?: return
        when (g.mine.kind) {
            RPS -> while (true) {
                val r = g.mine.reveals.size
                if (r >= g.mine.commits.size || r >= theirs.commits.size) return
                val secret = g.secrets.remove(r) ?: return
                g.mine.reveals.add(secret)
                changed()
            }
            BATTLESHIP -> {
                val layout = g.fleet ?: return
                if (g.mine.fleet.isEmpty()) return
                val answers = answerShots(shipCells(layout)!!, theirs.shots)
                if (answers != g.mine.answers) {
                    g.mine.answers = answers
                    changed()
                }
                if (g.mine.reveal == null) {
                    val (first, second) = halves(g)
                    if (first != null && second != null && first.fleet.isNotEmpty() && second.fleet.isNotEmpty()) {
                        val battle = replayBattle(listOf(first.shots, second.shots), listOf(first.answers, second.answers))
                        if (battle.winner != null) {
                            g.mine.reveal = layoutText(layout) to g.fleetSalt
                            changed()
                        }
                    }
                }
            }
        }
    }

    // ----- polling opponents ------------------------------------------------------------------------------

    private fun involvedWith(peerId: String): Boolean {
        if (invites.values.any { it.host == peerId }) return true
        return games.values.any {
            opponentId(it) == peerId && it.closedAt == null && outcome(it).status in setOf(GAME_WAITING, GAME_PLAYING)
        }
    }

    private fun run() {
        while (running) {
            try {
                val now = monotonicMs()
                for (peer in peers.online()) {
                    if (!peer.play || peer.peerId in inflight) continue
                    val quick = synchronized(lock) { involvedWith(peer.peerId) }
                    val interval = if (quick) ACTIVE_POLL else refreshMs
                    if (now - (polled[peer.peerId] ?: 0L) >= interval) {
                        polled[peer.peerId] = now
                        inflight.add(peer.peerId)
                        val url = peer.baseUrl()
                        pollers.execute { poll(peer.peerId, url) }
                    }
                }
                housekeeping()
            } catch (e: Exception) {
                CoreLog.e(TAG, "game polling failed", e)
            }
            Thread.sleep(200)
        }
    }

    private fun poll(peerId: String, baseUrl: String) {
        try {
            val path = "/api/games"
            val signed = auth.signRequest("GET", path)
            val builder = Request.Builder().url(baseUrl + path).header("Accept-Encoding", "identity")
            for ((k, v) in signed) builder.header(k, v)
            etags[peerId]?.let { builder.header("If-None-Match", it) }
            client.newCall(builder.build()).execute().use { resp ->
                if (resp.code != 200) return
                if (!auth.verifyResponse(signed[TOKEN_HEADER], resp.header(PROOF_HEADER))) return
                val body = readLimited(resp, MAX_PAYLOAD)
                merge(peerId, json.parseToJsonElement(body.toString(Charsets.UTF_8)))
                resp.header("etag")?.let { etags[peerId] = it }
            }
        } catch (e: Exception) {
            CoreLog.d(TAG, "could not read ${peerId.take(8)}'s games: $e")
        } finally {
            inflight.remove(peerId)
        }
    }

    /** Take in what a peer publishes about its games (only its own half of each counts). */
    fun merge(peerId: String, payload: JsonElement) {
        if (payload !is JsonObject || payload["peer_id"].str() != peerId) return
        val raw = payload["games"] as? JsonArray ?: return
        synchronized(lock) {
            val offered = HashSet<String>()
            for (item in raw.take(MAX_GAMES * 2)) {
                val record = parseGameRecord(item) ?: continue
                if (peerId !in record.players || me !in record.players) continue
                val g = games[record.id]
                if (g != null) {
                    if (g.mine.sameGame(record) && opponentId(g) == peerId && g.theirs != record) {
                        g.theirs = record
                        respond(g)
                        changed()
                    }
                    continue
                }
                if (record.host == peerId && record.stance == OFFERED) {
                    offered.add(record.id)
                    if (record.id !in invites) {
                        invites[record.id] = record
                        changed()
                        CoreLog.i(TAG, "${record.names[record.players.indexOf(peerId)]} invited us to ${GAME_TITLES[record.kind]}")
                    }
                }
            }
            val withdrawn = invites.filter { (id, r) -> r.host == peerId && id !in offered }.keys
            if (withdrawn.isNotEmpty()) {
                withdrawn.toList().forEach { invites.remove(it) }
                changed()
            }
        }
    }

    fun housekeeping() {
        val now = System.currentTimeMillis()
        val online = peers.online().map { it.peerId }.toSet()
        synchronized(lock) {
            for (g in games.values.toList()) {
                val closedAt = g.closedAt
                if (closedAt != null && now - closedAt > KEEP_CLOSED) {
                    games.remove(g.id)
                    changed()
                } else if (g.mine.host == me && g.mine.stance == OFFERED && now - g.created > INVITE_TTL &&
                    outcome(g).status == GAME_WAITING
                ) {
                    g.mine.stance = CANCELED_STANCE // nobody answered
                    changed()
                }
            }
            val gone = invites.filter { (_, r) -> r.host !in online }.keys
            if (gone.isNotEmpty()) {
                gone.toList().forEach { invites.remove(it) }
                changed()
            }
        }
    }

    /** For tests: my hidden fleet in a battleship game. */
    internal fun fleetOf(gameId: String): List<Pair<Int, Boolean>>? = synchronized(lock) { games[gameId]?.fleet }

    internal fun fleetSaltOf(gameId: String): String? = synchronized(lock) { games[gameId]?.fleetSalt }
}
