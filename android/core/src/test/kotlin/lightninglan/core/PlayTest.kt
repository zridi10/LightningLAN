package lightninglan.core

import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** The games' rules and records. Expected values come from the desktop app's lightninglan/play.py. */
class PlayTest {
    private val a = "a".repeat(32)
    private val b = "b".repeat(32)

    @Test
    fun `sealed choices match the desktop app's`() {
        assertEquals("35bebbcac1b3c667247d9777c13b77015cdc3435c1bcc4f8979bd5a3b7a61828", seal("1".repeat(32), 0, "rock", "0".repeat(32)))
        assertEquals("99f9f0cce11a492d353b0d72811603b762150fbf59b7dc7c65668c7a0034ddf4", seal("a".repeat(32), 4, "scissors", "f".repeat(32)))
    }

    @Test
    fun `moves only count in turn`() {
        assertEquals(listOf(0, 4, 1, 5, 2), interleave(listOf(0, 1, 2), listOf(4, 5)))
        assertEquals(listOf(0, 4, 1), interleave(listOf(0, 1, 2, 3), listOf(4)))
        val board = replayBoard(false, listOf(0, 1, 2), listOf(4, 5))
        assertEquals(0, board.winner)
        assertEquals(listOf(0, 1, 2), board.line)
        assertTrue(replayBoard(false, listOf(0, 2, 3, 7, 8), listOf(1, 4, 5, 6)).draw)
    }

    /** The same game the desktop app plays when it always picks legal[(i * k + c) % size]: same legal moves, same result. */
    private fun selfPlay(k: Int, c: Int): Pair<List<Int>, Board> {
        val board = Board(true)
        val moves = ArrayList<Int>()
        var i = 0
        while (!board.over) {
            val legal = board.legal()
            val m = legal[(i * k + c) % legal.size]
            assertTrue(board.play(m))
            moves.add(m)
            i++
        }
        return moves to board
    }

    @Test
    fun `ultimate games play out exactly as on the desktop`() {
        val (drawn, drawBoard) = selfPlay(7, 3)
        assertEquals(
            listOf(3, 28, 17, 78, 58, 38, 18, 5, 50, 47, 20, 23, 49, 43, 65, 19, 12, 30, 32, 52, 71, 79, 67, 40, 41, 51, 55, 13, 44,
                75, 33, 59, 53, 72, 4, 36, 6, 61, 70, 64, 10, 9, 2, 26, 16, 68, 46, 15, 62, 14, 48, 27, 31, 42, 56, 21, 35, 54, 24, 57,
                34, 66, 69, 60, 22, 11),
            drawn,
        )
        assertTrue(drawBoard.draw)
        assertEquals("xoxxoxoxo", drawBoard.small.joinToString("") { it.take(1).ifEmpty { "." } })

        val (won, winBoard) = selfPlay(3, 2)
        assertEquals(
            listOf(2, 23, 53, 74, 25, 71, 77, 52, 65, 19, 14, 45, 7, 70, 66, 29, 21, 33, 56, 26, 80, 79, 68, 51, 57, 27, 4, 38, 22,
                37, 13, 42, 54, 8, 78, 62, 75, 35, 76, 44, 9, 0, 1, 17, 20, 24, 55, 15, 12, 34, 63, 36, 50, 47, 18, 69, 49, 48),
            won,
        )
        assertEquals(1, winBoard.winner)
        assertEquals(listOf(3, 4, 5), winBoard.line)
        assertEquals("xxdoooxox", winBoard.small.joinToString("") { it.take(1).ifEmpty { "." } })
        // Replayed from the two players' halves, as peers do.
        val replayed = replayBoard(true, won.filterIndexed { i, _ -> i % 2 == 0 }, won.filterIndexed { i, _ -> i % 2 == 1 })
        assertEquals(winBoard.cells, replayed.cells)
    }

    @Test
    fun `rock paper scissors catches a changed choice`() {
        val id = "1".repeat(32)
        val commits = listOf(listOf(seal(id, 0, "rock", "0".repeat(32))), listOf(seal(id, 0, "paper", "1".repeat(32))))
        val honest = replayRounds(id, 1, commits, listOf(listOf("rock" to "0".repeat(32)), listOf("paper" to "1".repeat(32))))
        assertEquals(1, honest.winner)
        val changed = replayRounds(id, 1, commits, listOf(listOf("rock" to "0".repeat(32)), listOf("scissors" to "1".repeat(32))))
        assertEquals(1, changed.cheat)
        assertEquals(0, changed.winner)
    }

    @Test
    fun `records from other peers are validated`() {
        val good = """{"id":"${"1".repeat(32)}","kind":"tictactoe","players":["$a","$b"],"names":["A","B"],"host":"$a",
            |"best_of":1,"stance":"offered","moves":[4]}""".trimMargin()
        assertEquals(listOf(4), parseGameRecord(Json.parseToJsonElement(good))!!.moves)
        for (bad in listOf(good.replace("tictactoe", "chess"), good.replace("[4]", "[9]"), good.replace("[4]", "[true]"),
            good.replace("\"$b\"", "\"$a\""), good.replace("offered", "winning"))) {
            assertNull(parseGameRecord(Json.parseToJsonElement(bad)), bad)
        }
        assertNull(parseGameRecord(Json.parseToJsonElement(good.replace("tictactoe", "rps").replace("\"best_of\":1", "\"best_of\":4"))))
    }

    // ----- two players, without a network --------------------------------------------------------

    private fun arcade(me: String, name: String, other: String, otherName: String): Arcade {
        val auth = Authenticator(null, me)
        val table = PeerTable(me, auth, 3_000, OkHttpClient())
        table.serviceFound(ServiceRecord("svc-$other", other, otherName, listOf("127.0.0.1"), 1, false))
        table.get(other)!!.apply {
            state = ONLINE
            play = true
        }
        return Arcade(me, { name }, auth, table, 3_000, OkHttpClient())
    }

    private fun sync(x: Arcade, xId: String, y: Arcade, yId: String) {
        repeat(2) {
            y.merge(xId, Json.parseToJsonElement(x.published().first.toString(Charsets.UTF_8)))
            x.merge(yId, Json.parseToJsonElement(y.published().first.toString(Charsets.UTF_8)))
        }
    }

    @Test
    fun `back-to-back games take turns going first, and a first game picks at random`() {
        val alice = arcade(a, "Alice", b, "Bob")
        val bob = arcade(b, "Bob", a, "Alice")
        val id = alice.invite(b, TICTACTOE, first = true).id
        sync(alice, a, bob, b)
        bob.answer(id, true)
        sync(alice, a, bob, b)
        val next = alice.invite(b, ULTIMATE) // any game, from either side: the other one starts
        assertEquals(1, next.seat)
        sync(alice, a, bob, b)
        bob.answer(next.id, true)
        sync(alice, a, bob, b)
        assertEquals(1, bob.invite(a, RPS).seat) // Bob started the last one
        assertEquals(0, alice.invite(b, TICTACTOE).seat) // that invitation wasn't accepted: it doesn't count
        val seats = (1..40).map { arcade(a, "Alice", b, "Bob").invite(b, TICTACTOE).seat }.toSet()
        assertEquals(setOf(0, 1), seats)
    }

    @Test
    fun `two players invite, play and finish`() {
        val alice = arcade(a, "Alice", b, "Bob")
        val bob = arcade(b, "Bob", a, "Alice")
        val id = alice.invite(b, TICTACTOE, first = true).id
        sync(alice, a, bob, b)
        assertEquals("Alice", bob.view().invites.single().from.name)
        assertNotNull(bob.answer(id, true))
        sync(alice, a, bob, b)
        assertTrue(alice.view().games.single().yourTurn)
        assertFailsWith<PlayError> { bob.move(id, 4) }
        for ((who, cell) in listOf(alice to 0, bob to 4, alice to 1, bob to 5, alice to 2)) {
            who.move(id, cell)
            sync(alice, a, bob, b)
        }
        assertEquals("won", alice.view().games.single().result)
        assertEquals("lost", bob.view().games.single().result)
    }

    @Test
    fun `rock paper scissors stays sealed until both lock in`() {
        val alice = arcade(a, "Alice", b, "Bob")
        val bob = arcade(b, "Bob", a, "Alice")
        val id = alice.invite(b, RPS, bestOf = 1, first = true).id
        sync(alice, a, bob, b)
        bob.answer(id, true)
        sync(alice, a, bob, b)
        alice.choose(id, "rock")
        sync(alice, a, bob, b)
        assertTrue("\"reveals\":[]" in alice.published().first.toString(Charsets.UTF_8))
        assertTrue(bob.view().games.single().rps!!.theyLocked)
        bob.choose(id, "paper")
        sync(alice, a, bob, b)
        assertEquals(listOf(RoundView("rock", "paper", "lost")), alice.view().games.single().rps!!.rounds)
        assertEquals("won", bob.view().games.single().result)
    }
}
