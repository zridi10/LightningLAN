package lightninglan.app.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.delay

/*
 * Falling blocks: the solo game in While you wait (a Tetris-style game with the same rules as
 * the computer's page, static/blocks.js). Everything happens on the phone; only the best score
 * is remembered.
 */

private const val COLS = 10
private const val ROWS = 20
private val PIECES = mapOf(
    'I' to listOf("....", "IIII", "....", "...."),
    'O' to listOf("OO", "OO"),
    'T' to listOf(".T.", "TTT", "..."),
    'S' to listOf(".SS", "SS.", "..."),
    'Z' to listOf("ZZ.", ".ZZ", "..."),
    'J' to listOf("J..", "JJJ", "..."),
    'L' to listOf("..L", "LLL", "..."),
)
private val COLORS = mapOf(
    'I' to Color(0xFF22D3EE), 'O' to Color(0xFFFACC15), 'T' to Color(0xFFA855F7), 'S' to Color(0xFF22C55E),
    'Z' to Color(0xFFEF4444), 'J' to Color(0xFF3B82F6), 'L' to Color(0xFFF97316),
)
private val BOARD = Color(0xFF0F172A)
private val LINE_POINTS = listOf(0, 100, 300, 500, 800)
private val KICKS = listOf(0, -1, 1, -2, 2)

private typealias Shape = List<List<Char?>>

private fun shapeOf(kind: Char): Shape = PIECES.getValue(kind).map { row -> row.map { if (it == '.') null else it } }

private fun rotate(m: Shape, clockwise: Boolean): Shape {
    val n = m.size
    return List(n) { r -> List(n) { c -> if (clockwise) m[n - 1 - c][r] else m[c][n - 1 - r] } }
}

private class Piece(val kind: Char, var shape: Shape, var x: Int, var y: Int)

/** The game itself. What the screen shows is Compose state, so changes redraw it. */
private class Blocks(best: Int, private val saveBest: (Int) -> Unit) {
    val grid = Array(ROWS) { arrayOfNulls<Char>(COLS) }
    var piece: Piece? = null
        private set
    var next by mutableStateOf<Char?>(null)
        private set
    private val bag = ArrayList<Char>()
    var score by mutableIntStateOf(0)
        private set
    var lines by mutableIntStateOf(0)
        private set
    var level by mutableIntStateOf(1)
        private set
    var best by mutableIntStateOf(best)
        private set
    var running by mutableStateOf(false)
        private set
    var paused by mutableStateOf(false)
    var over by mutableStateOf(false)
        private set
    /** Bumped on every change to the board or the falling piece, so the board redraws. */
    var version by mutableIntStateOf(0)
        private set

    val playing: Boolean get() = running && !paused && !over && piece != null
    val interval: Long get() = maxOf(70L, 800L - (level - 1) * 70L)

    private fun fromBag(): Char {
        if (bag.isEmpty()) bag.addAll(PIECES.keys.shuffled())
        return bag.removeAt(bag.size - 1)
    }

    fun collides(m: Shape, x: Int, y: Int): Boolean {
        for (r in m.indices) for (c in m[r].indices) {
            if (m[r][c] == null) continue
            val bx = x + c
            val by = y + r
            if (bx < 0 || bx >= COLS || by >= ROWS) return true
            if (by >= 0 && grid[by][bx] != null) return true
        }
        return false
    }

    private fun spawn() {
        val kind = next ?: fromBag()
        next = fromBag()
        val shape = shapeOf(kind)
        val p = Piece(kind, shape, (COLS - shape.size) / 2, if (kind == 'I') -1 else 0)
        if (collides(p.shape, p.x, p.y)) {
            over = true
            running = false
            piece = null
            if (score > best) {
                best = score
                saveBest(score)
            }
        } else {
            piece = p
        }
    }

    fun start() {
        for (row in grid) row.fill(null)
        bag.clear()
        next = null
        score = 0
        lines = 0
        level = 1
        over = false
        paused = false
        running = true
        spawn()
        version++
    }

    private fun lock() {
        val p = piece ?: return
        for (r in p.shape.indices) for (c in p.shape[r].indices) {
            val v = p.shape[r][c] ?: continue
            if (p.y + r >= 0) grid[p.y + r][p.x + c] = v
        }
        var cleared = 0
        var r = ROWS - 1
        while (r >= 0) {
            if (grid[r].all { it != null }) {
                for (k in r downTo 1) grid[k] = grid[k - 1].copyOf()
                grid[0] = arrayOfNulls(COLS)
                cleared++
            } else {
                r--
            }
        }
        if (cleared > 0) {
            score += LINE_POINTS[cleared] * level
            lines += cleared
            level = 1 + lines / 10
        }
        spawn()
    }

    fun shift(dx: Int) {
        val p = piece ?: return
        if (playing && !collides(p.shape, p.x + dx, p.y)) {
            p.x += dx
            version++
        }
    }

    fun turn(clockwise: Boolean) {
        val p = piece ?: return
        if (!playing) return
        val m = rotate(p.shape, clockwise)
        val kick = KICKS.firstOrNull { !collides(m, p.x + it, p.y) } ?: return
        p.shape = m
        p.x += kick
        version++
    }

    fun fall(byPlayer: Boolean) {
        val p = piece ?: return
        if (!playing) return
        if (!collides(p.shape, p.x, p.y + 1)) {
            p.y++
            if (byPlayer) score++
        } else {
            lock()
        }
        version++
    }

    fun drop() {
        val p = piece ?: return
        if (!playing) return
        var n = 0
        while (!collides(p.shape, p.x, p.y + 1)) {
            p.y++
            n++
        }
        score += 2 * n
        lock()
        version++
    }

    fun ghostY(): Int? {
        val p = piece ?: return null
        var y = p.y
        while (!collides(p.shape, p.x, y + 1)) y++
        return y
    }
}

/** Kept for as long as the app runs, so switching tabs doesn't lose a game (it pauses instead). */
private var keptGame: Blocks? = null

private fun DrawScope.block(x: Float, y: Float, s: Float, color: Color) {
    drawRect(color, Offset(x + 1, y + 1), Size(s - 2, s - 2))
    drawRect(Color.White.copy(alpha = 0.25f), Offset(x + 1, y + 1), Size(s - 2, maxOf(2f, s / 6)))
}

@Composable
internal fun FallingBlocks(best: Int, saveBest: (Int) -> Unit) {
    val game = keptGame ?: Blocks(best, saveBest).also { keptGame = it }
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    LaunchedEffect(game) {
        lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
            try {
                while (true) {
                    delay(game.interval)
                    game.fall(false)
                }
            } finally {
                if (game.playing) game.paused = true // leaving the app or the tab pauses the game
            }
        }
    }
    val message = when {
        game.over -> "Game over: ${game.score} points." + if (game.score > 0 && game.score == game.best) " A new best!" else ""
        game.paused -> "Paused."
        game.running -> "Fill whole rows to clear them. Every 10 rows, it gets faster."
        else -> "Stack the falling blocks and fill whole rows to clear them."
    }
    val startLabel = when {
        game.over -> "Again"
        !game.running -> "Start"
        game.paused -> "Resume"
        else -> "Pause"
    }
    Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text(message, textAlign = TextAlign.Center)
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.Top) {
            Canvas(Modifier.widthIn(max = 240.dp).weight(1f, fill = false).aspectRatio(COLS / ROWS.toFloat()).clip(RoundedCornerShape(8.dp))) {
                game.version // redraw on every change
                val s = size.width / COLS
                drawRect(BOARD)
                for (c in 1 until COLS) drawLine(Color.White.copy(alpha = 0.06f), Offset(c * s, 0f), Offset(c * s, size.height))
                for (r in 1 until ROWS) drawLine(Color.White.copy(alpha = 0.06f), Offset(0f, r * s), Offset(size.width, r * s))
                for (r in 0 until ROWS) for (c in 0 until COLS) game.grid[r][c]?.let { block(c * s, r * s, s, COLORS.getValue(it)) }
                val p = game.piece
                val ghost = game.ghostY()
                if (p != null && ghost != null) {
                    for (r in p.shape.indices) for (c in p.shape[r].indices) {
                        val v = p.shape[r][c] ?: continue
                        if (ghost + r >= 0) {
                            drawRect(Color.White.copy(alpha = 0.35f), Offset((p.x + c) * s + 2, (ghost + r) * s + 2), Size(s - 4, s - 4),
                                style = Stroke(1.5f))
                        }
                        if (p.y + r >= 0) block((p.x + c) * s, (p.y + r) * s, s, COLORS.getValue(v))
                    }
                }
                if (!game.playing) drawRect(BOARD.copy(alpha = 0.55f))
            }
            Column(verticalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.width(100.dp)) {
                Text("Next", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                val next = game.next
                Canvas(Modifier.size(72.dp).clip(RoundedCornerShape(6.dp))) {
                    drawRect(BOARD)
                    if (next != null) {
                        val m = shapeOf(next)
                        val s = size.width / 4
                        val off = (4 - m.size) * s / 2
                        for (r in m.indices) for (c in m[r].indices) m[r][c]?.let { block(off + c * s, off + r * s, s, COLORS.getValue(it)) }
                    }
                }
                for ((label, value) in listOf("Score" to game.score, "Rows" to game.lines, "Level" to game.level,
                    "Best" to maxOf(game.best, game.score))) {
                    Row {
                        Text(label, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.weight(1f))
                        Text("$value", fontWeight = FontWeight.Bold)
                    }
                }
                ThemedButton(onClick = { if (!game.running) game.start() else game.paused = !game.paused }) { Text(startLabel) }
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            val keys = listOf<Pair<String, () -> Unit>>(
                "◀" to { game.shift(-1) }, "↺" to { game.turn(false) }, "↻" to { game.turn(true) },
                "▼" to { game.fall(true) }, "⤓" to { game.drop() }, "▶" to { game.shift(1) },
            )
            for ((label, action) in keys) {
                OutlinedButton(onClick = action, enabled = game.playing, contentPadding = PaddingValues(0.dp),
                    modifier = Modifier.size(50.dp)) { Text(label, fontSize = 20.sp) }
            }
        }
        Text("◀ ▶ move  ·  ↺ ↻ turn  ·  ▼ down  ·  ⤓ drop",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
