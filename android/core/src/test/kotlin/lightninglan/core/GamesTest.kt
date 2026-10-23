package lightninglan.core

import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** The rules of the new games, with the desktop app's own expected values (tests/test_games.py). */
class GamesTest {
    private val gid = "0123456789abcdef".repeat(2)

    @Test
    fun `four in a row finds the same lines`() {
        assertEquals(listOf(17, 24, 31, 38), replayTurns(FourInARow(), listOf(3, 3, 3, 3), listOf(4, 4, 4)).line)
        val diagonal = replayTurns(FourInARow(), listOf(0, 1, 2, 3, 2, 3), listOf(1, 2, 3, 6, 3))
        assertEquals(0, diagonal.winner)
        assertEquals(listOf(17, 23, 29, 35), diagonal.line)
    }

    private fun blankCheckers(): Checkers = Checkers().apply {
        for (i in 0 until 64) board[i] = ""
        forget()
    }

    @Test
    fun `checkers jumps are compulsory, chain, and crown`() {
        assertEquals(7, Checkers().legal().size)
        val game = blankCheckers()
        game.board[45] = "x"
        game.board[36] = "o"
        game.board[20] = "o"
        game.board[60] = "x"
        game.board[7] = "o"
        game.forget()
        assertEquals(listOf(listOf(45, 27, 13)), game.legal())
        assertFalse(game.play(listOf(60, 51)))
        assertTrue(game.play(listOf(45, 27, 13)))
        assertEquals("", game.board[36])
        val crown = blankCheckers()
        crown.board[19] = "x"
        crown.board[10] = "o"
        crown.board[63] = "o"
        crown.forget()
        assertTrue(crown.play(listOf(19, 1)))
        assertEquals("X", crown.board[1])
        val last = blankCheckers()
        last.board[45] = "x"
        last.board[36] = "o"
        last.forget()
        assertTrue(last.play(listOf(45, 27)))
        assertEquals(0, last.winner)
    }

    @Test
    fun `dots and boxes gives another turn for a box`() {
        assertEquals(listOf(0, 5, 30, 31), boxLines(0))
        assertEquals(listOf(0, 5), boxesOf(5).sorted())
        val game = Dots()
        for (line in listOf(0, 5, 30)) assertTrue(game.play(line))
        assertTrue(game.play(31))
        assertEquals("o", game.boxes[0])
        assertEquals(1, game.turn)
        while (!game.over) game.play(game.legal().first())
        assertEquals(25, game.score.sum())
    }

    @Test
    fun `memory cards are shuffled exactly as on the desktop`() {
        assertEquals(listOf(2, 4, 7, 5, 1, 3, 0, 4, 6, 2, 6, 7, 3, 1, 0, 5), memoryLayout(gid))
        assertEquals(listOf(2, 0, 7, 1, 1, 4, 5, 6, 4, 7, 2, 6, 3, 0, 3, 5), memoryLayout("f".repeat(32)))
        val game = Memory(listOf(0, 0, 1, 1) + (0 until 12).map { 2 + it / 2 })
        assertTrue(game.play(0) && game.play(1))
        assertEquals(0, game.turn)
        assertTrue(game.play(2) && game.play(4))
        assertEquals(listOf(2, 4), game.shown)
        assertEquals(1, game.turn)
    }

    @Test
    fun `battleship fleets and answers`() {
        val layout = listOf(0 to true, 20 to true, 40 to true, 60 to true, 80 to true)
        val ships = shipCells(layout)!!
        assertEquals(listOf(80, 81), ships[4])
        assertEquals("0h,20h,40h,60h,80h", layoutText(layout))
        assertEquals(layout, parseLayout("0h,20h,40h,60h,80h"))
        assertNull(shipCells(listOf(8 to true) + layout.drop(1)))
        assertNull(shipCells(listOf(0 to false) + layout.drop(1)))
        assertTrue(shipCells(randomLayout(Random(1))) != null)
        assertEquals(listOf("miss", "hit", "sunk", "hit"), answerShots(ships, listOf(5, 80, 81, 0)))
        val battle = replayBattle(listOf(listOf(5, 6), listOf(7)), listOf(listOf("miss"), listOf("miss", "hit")))
        assertEquals(listOf(2, 1), battle.fired.toList())
        assertEquals(listOf(0, 1), battle.hits.toList())
        assertEquals(1, battle.turn)
        assertEquals("3d66468321a7b31134fad243574a95869977685ba3a57c5a6506fc892a4e5243", sealFleet(gid, layoutText(layout), "0".repeat(32)))
    }

    @Test
    fun `sums and sentences come from the game id exactly as on the desktop`() {
        assertEquals(listOf("57 − 23" to 34, "11 × 12" to 132, "8 × 8" to 64), mathProblems(gid).take(3))
        assertEquals(SENTENCES[13], sentenceFor(gid))
        val race = replayRace(listOf(listOf(900, 1200, 800), listOf(1000, 1000, 800)), 3)
        assertEquals(listOf(0, 1, null), race.rounds)
        assertTrue(race.draw)
    }
}
