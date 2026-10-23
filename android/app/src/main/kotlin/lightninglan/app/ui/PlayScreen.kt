@file:OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)

package lightninglan.app.ui

import android.os.SystemClock
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import lightninglan.app.Sharing
import lightninglan.core.BATTLESHIP
import lightninglan.core.BEST_OF
import lightninglan.core.C4_COLS
import lightninglan.core.C4_ROWS
import lightninglan.core.CHECKERS
import lightninglan.core.CONNECT4
import lightninglan.core.DOTS
import lightninglan.core.DOTS_SIZE
import lightninglan.core.FLEET
import lightninglan.core.GAME_OVER
import lightninglan.core.GAME_PLAYING
import lightninglan.core.GAME_TITLES
import lightninglan.core.GAME_WAITING
import lightninglan.core.GRID
import lightninglan.core.GameView
import lightninglan.core.InviteView
import lightninglan.core.MATH
import lightninglan.core.MEMORY
import lightninglan.core.Opponent
import lightninglan.core.PlayView
import lightninglan.core.RPS
import lightninglan.core.TICTACTOE
import lightninglan.core.TYPING
import lightninglan.core.ULTIMATE
import kotlin.math.abs
import kotlin.math.hypot

/*
 * While you wait: head-to-head games with the other devices, the same games as on the
 * computer's page (static/play.js), and a solo game (BlocksGame.kt). The rules and the other
 * player's moves are handled by the core (Play.kt); this only shows them.
 */

private val CHOICE_ICON = mapOf("rock" to "✊", "paper" to "✋", "scissors" to "✌️")
// The memory match pictures, in the same order as play.js.
private val CARD_PICTURES = listOf("🍎", "🍋", "🍇", "🍉", "🍒", "🥝", "🍍", "🥥")
private val ABOUT = mapOf(
    TICTACTOE to "The classic: three in a row on a 3×3 grid.",
    ULTIMATE to "Nine boards in one. Where you play picks the board your opponent must play in next. Win three boards in a row.",
    CONNECT4 to "Drop discs into seven columns. First to four in a row, in any direction, wins.",
    CHECKERS to "Move diagonally, jump to capture (jumps are a must, and chains keep going), and reach the far side to be " +
        "crowned. Take every piece, or leave them no move.",
    DOTS to "Take turns drawing a line between two dots. Close a box to claim it and go again. Most boxes wins.",
    BATTLESHIP to "Hide five ships, then take turns firing. Every shot gets a hit or a miss. Sink the whole fleet first. " +
        "Both fleets are checked at the end, so nobody can fib.",
    MEMORY to "Take turns turning over two cards. Find a pair to keep it and go again. Most pairs wins.",
    RPS to "Both lock in a choice, then both are revealed together. Rock beats scissors, scissors beat paper, paper beats rock.",
    TYPING to "The same sentence for both of you. Fastest accurate finish wins. Each side times itself.",
    MATH to "Ten quick sums. Whoever gets each one right faster gets the point. Each side times itself.",
)
private val COLOR_NAME = mapOf(CONNECT4 to mapOf("x" to "red", "o" to "yellow"), CHECKERS to mapOf("x" to "red", "o" to "black"))

private val C4_FRAME = Color(0xFF1F4FD6)
private val C4_DISC = mapOf("x" to Color(0xFFE5484D), "o" to Color(0xFFF5C518))
private val CHECKERS_LIGHT = Color(0xFFF0D9B5)
private val CHECKERS_DARK = Color(0xFFB58863)
private val CHECKERS_EDGE = Color(0xFF5B3A1E)
private val SEA = Color(0xFF0B5C8A)
private val SHIP = Color(0xFF8A99A8)
private val HIT = Color(0xFFE5484D)
private val SUNK = Color(0xFF7A1018)
private val SPLASH = Color(0xFFE8F4FF)

/** "12.34 s" (the same as the computer's page). */
private fun seconds(ms: Int): String = "${ms / 1000}.${(ms % 1000 / 10).toString().padStart(2, '0')} s"

/** How many things wait for you: invitations, and games where it's your move (the tab's badge). */
fun waitingForYou(play: PlayView?): Int = play?.let { p -> p.invites.size + p.games.count { it.yourTurn } } ?: 0

private fun choiceName(choice: String) = choice.replaceFirstChar { it.uppercase() }

private fun resultText(g: GameView): String {
    val reason = if (g.reason.isNotEmpty()) " ${g.reason}." else ""
    return when (g.result) {
        "won" -> "You won!$reason"
        "lost" -> "${g.opponent.name} won.$reason"
        else -> "It’s a draw.$reason"
    }
}

private fun statusText(g: GameView): String {
    val who = g.opponent.name
    val away = if (g.opponent.online) "" else " ($who is offline right now)"
    return when (g.status) {
        GAME_WAITING -> "Waiting for $who to accept…$away"
        GAME_OVER -> resultText(g)
        GAME_PLAYING -> when (g.kind) {
            RPS ->
                if (g.yourTurn) "Pick one!" + (if (g.rps?.theyLocked == true) " $who has locked in." else "") + away
                else "You picked ${g.rps?.choice?.let(::choiceName) ?: "yours"}. Waiting for $who…$away"
            BATTLESHIP -> {
                val b = g.battle
                when {
                    b == null || b.phase == "setup" ->
                        if (b?.ready == true) "Waiting for $who to place their ships…$away"
                        else "Place your ships (shuffle until you like them), then press Ready."
                    g.yourTurn -> "Your shot: pick a square in $who’s waters.$away"
                    else -> "$who is aiming…$away"
                }
            }
            TYPING -> g.race?.myTime?.let { "You took ${seconds(it)}. Waiting for $who to finish…$away" }
                ?: "Type the sentence as fast as you can: the clock starts when it appears."
            MATH -> {
                val s = g.sprint
                if (s == null || s.myTimes.size < s.problems.size) "Answer ten sums as fast as you can."
                else "All done! Waiting for $who (${s.theirCount} of ${s.problems.size})…$away"
            }
            MEMORY -> when {
                !g.yourTurn -> "$who is turning cards…$away"
                g.pending == null -> "Your turn: turn over two cards."
                else -> "Now pick a second card."
            }
            DOTS -> if (g.yourTurn) "Your move: draw a line. Close a box to go again.$away" else "$who’s move…$away"
            CONNECT4 -> if (g.yourTurn) "You’re ${COLOR_NAME.getValue(CONNECT4)[g.mark]}. Pick a column.$away" else "$who’s move…$away"
            CHECKERS -> when {
                !g.yourTurn -> "$who’s move…$away"
                else -> {
                    val jump = g.paths.firstOrNull()?.let { abs(it[1] / 8 - it[0] / 8) == 2 } == true
                    "You’re ${COLOR_NAME.getValue(CHECKERS)[g.mark]}. " + if (jump) "You must jump." else "Your move."
                }
            }
            else -> when {
                !g.yourTurn -> "$who’s move…$away"
                g.kind == ULTIMATE -> "You’re ${g.mark.uppercase()}. " +
                    if (g.forced == null) "Your move: any open board." else "Your move, in the highlighted board."
                else -> "You’re ${g.mark.uppercase()}. Your move."
            }
        }
        else -> "${g.reason}."
    }
}

@Composable
internal fun PlayPanel(sharing: Sharing, play: PlayView?, online: Boolean, padding: PaddingValues) {
    var solo by rememberSaveable { mutableStateOf(false) }
    var openId by rememberSaveable { mutableStateOf<String?>(null) }
    val open = play?.games?.firstOrNull { it.id == openId }
    LazyColumn(
        contentPadding = PaddingValues(16.dp, padding.calculateTopPadding() + 4.dp, 16.dp, padding.calculateBottomPadding() + 24.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item(key = "head") {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("While you wait", style = headingStyle())
                Spacer(Modifier.weight(1f))
                FilterChip(selected = !solo, onClick = { solo = false }, label = { Text("Head-to-head") })
                Spacer(Modifier.width(6.dp))
                FilterChip(selected = solo, onClick = { solo = true }, label = { Text("Solo") })
            }
        }
        if (solo) {
            item(key = "solo") {
                PlayCard {
                    Text("Falling blocks", fontWeight = FontWeight.SemiBold)
                    FallingBlocks(sharing.settings.blocksBest) { sharing.settings.blocksBest = it }
                }
            }
            return@LazyColumn
        }
        if (play == null) {
            item(key = "none") { Note(if (online) "Getting ready…" else "Start sharing to play with your other devices.") }
            return@LazyColumn
        }
        items(play.invites, key = { "i" + it.id }) { invite -> InviteCard(sharing, invite) { openId = it } }
        if (open != null) item(key = "open") { GameCard(sharing, open, onBack = { openId = null }, onOpen = { openId = it }) }
        val others = play.games.filter { it.id != open?.id }
        if (others.isNotEmpty()) {
            item(key = "games") { SubHeader("Your games") }
            items(others, key = { "g" + it.id }) { g -> GameRow(sharing, g) { openId = g.id } }
        }
        item(key = "start") { SubHeader("Start a game") }
        if (play.opponents.isEmpty()) {
            item(key = "nobody") {
                Note("Nobody to play yet. Games need the latest LightningLAN on the other computer or phone, on the same Wi-Fi.")
            }
        }
        items(GAME_TITLES.keys.toList(), key = { "k$it" }) { kind -> KindCard(sharing, kind, play.opponents, online) { openId = it } }
    }
}

@Composable
private fun SubHeader(text: String) {
    Text(text, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(top = 6.dp))
}

@Composable
private fun Note(text: String) {
    Text(text, color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodyMedium)
}

@Composable
private fun PlayCard(highlight: Boolean = false, content: @Composable () -> Unit) {
    Card(
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer),
        border = if (highlight) androidx.compose.foundation.BorderStroke(1.dp, MaterialTheme.colorScheme.primary) else null,
    ) {
        Column(Modifier.padding(14.dp).fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) { content() }
    }
}

@Composable
private fun InviteCard(sharing: Sharing, invite: InviteView, onOpen: (String) -> Unit) {
    PlayCard(highlight = true) {
        val extra = when {
            invite.kind == RPS -> " (best of ${invite.bestOf})."
            invite.kind == TYPING || invite.kind == MATH -> "."
            invite.youGoFirst -> ". You go first."
            else -> ". They go first."
        }
        Text("${invite.from.name} challenges you to ${invite.title}$extra")
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ThemedButton(onClick = { sharing.answer(invite.id, true) { onOpen(it.id) } }) { Text("Play") }
            OutlinedButton(onClick = { sharing.answer(invite.id, false) {} }) { Text("No thanks") }
        }
    }
}

@Composable
private fun GameRow(sharing: Sharing, g: GameView, onOpen: () -> Unit) {
    PlayCard(highlight = g.yourTurn) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("${g.title} vs ${g.opponent.name}", fontWeight = FontWeight.Medium)
                Text(statusText(g), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (g.status == GAME_PLAYING || g.status == GAME_WAITING || g.status == GAME_OVER) {
                if (g.yourTurn) ThemedButton(onClick = onOpen) { Text("Play") } else OutlinedButton(onClick = onOpen) { Text("Open") }
            }
            if (g.status != GAME_PLAYING && g.status != GAME_WAITING) {
                TextButton(onClick = { sharing.closeGame(g.id) }) { Text("Close") }
            }
        }
    }
}

@Composable
private fun GameCard(sharing: Sharing, g: GameView, onBack: () -> Unit, onOpen: (String) -> Unit) {
    var confirmResign by remember { mutableStateOf(false) }
    val style = LocalAppStyle.current
    PlayCard {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(g.title + if (g.kind == RPS) " · best of ${g.bestOf}" else "", fontWeight = FontWeight.SemiBold)
                Text("vs ${g.opponent.name}", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            TextButton(onClick = onBack) { Text("All games") }
        }
        Text(
            statusText(g),
            fontWeight = FontWeight.Medium,
            textAlign = TextAlign.Center,
            modifier = Modifier.fillMaxWidth(),
            color = when (g.result) {
                "won" -> style.ok
                "lost" -> MaterialTheme.colorScheme.error
                else -> MaterialTheme.colorScheme.onSurface
            },
        )
        Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
            when (g.kind) {
                TICTACTOE -> TicTacToeBoard(sharing, g)
                ULTIMATE -> UltimateBoard(sharing, g)
                CONNECT4 -> FourInARowBoard(sharing, g)
                CHECKERS -> CheckersBoard(sharing, g)
                DOTS -> DotsBoard(sharing, g)
                BATTLESHIP -> BattleshipBoard(sharing, g)
                MEMORY -> MemoryBoard(sharing, g)
                TYPING -> TypingRace(sharing, g)
                MATH -> MathSprint(sharing, g)
                else -> RockPaperScissors(sharing, g)
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            when (g.status) {
                GAME_PLAYING -> TextButton(onClick = { confirmResign = true }) { Text("Resign", color = MaterialTheme.colorScheme.error) }
                GAME_WAITING -> TextButton(onClick = { sharing.withdraw(g.id) }) { Text("Withdraw") }
                else -> {
                    ThemedButton(onClick = { sharing.rematch(g.id) { onOpen(it.id) } }) { Text("Rematch") }
                    TextButton(onClick = { onBack(); sharing.closeGame(g.id) }) { Text("Close") }
                }
            }
        }
    }
    if (confirmResign) {
        AlertDialog(
            onDismissRequest = { confirmResign = false },
            containerColor = style.solidSurface,
            title = { Text("Give up this game?") },
            confirmButton = { TextButton(onClick = { confirmResign = false; sharing.resign(g.id) }) { Text("Resign") } },
            dismissButton = { TextButton(onClick = { confirmResign = false }) { Text("Keep playing") } },
        )
    }
}

// ----- boards ----------------------------------------------------------------------------------------

@Composable
private fun Cell(sharing: Sharing, g: GameView, index: Int, modifier: Modifier, fontSize: TextUnit, win: Boolean, faded: Boolean = false) {
    val mark = g.board[index]
    val playable = index in g.allowed
    val colors = MaterialTheme.colorScheme
    val shape = RoundedCornerShape(if (fontSize.value > 24) 8.dp else 4.dp)
    Box(
        modifier
            .clip(shape)
            .background(
                when {
                    win -> LocalAppStyle.current.ok.copy(alpha = 0.25f)
                    playable -> colors.primary.copy(alpha = 0.16f)
                    else -> colors.surfaceContainerHigh
                },
            )
            .border(if (g.last == index) 2.dp else 1.dp, if (g.last == index) colors.onSurfaceVariant else colors.outlineVariant, shape)
            .clickable(enabled = playable) { sharing.move(g.id, index) },
        contentAlignment = Alignment.Center,
    ) {
        if (mark.isNotEmpty()) {
            Text(
                mark.uppercase(),
                fontSize = fontSize,
                fontWeight = FontWeight.Bold,
                color = (if (mark == "x") colors.primary else colors.tertiary).copy(alpha = if (faded) 0.3f else 1f),
            )
        }
    }
}

@Composable
private fun TicTacToeBoard(sharing: Sharing, g: GameView) {
    Column(Modifier.widthIn(max = 320.dp).fillMaxWidth().aspectRatio(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        for (row in 0 until 3) {
            Row(Modifier.weight(1f), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                for (col in 0 until 3) {
                    val i = row * 3 + col
                    Cell(sharing, g, i, Modifier.weight(1f).fillMaxHeight(), 40.sp, win = i in g.line)
                }
            }
        }
    }
}

@Composable
private fun UltimateBoard(sharing: Sharing, g: GameView) {
    val colors = MaterialTheme.colorScheme
    Column(Modifier.widthIn(max = 460.dp).fillMaxWidth().aspectRatio(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        for (boardRow in 0 until 3) {
            Row(Modifier.weight(1f), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                for (boardCol in 0 until 3) {
                    val b = boardRow * 3 + boardCol
                    val owner = g.small.getOrElse(b) { "" }
                    val active = g.allowed.any { it / 9 == b }
                    val shape = RoundedCornerShape(8.dp)
                    Box(
                        Modifier
                            .weight(1f)
                            .fillMaxHeight()
                            .clip(shape)
                            .background(if (b in g.line) LocalAppStyle.current.ok.copy(alpha = 0.2f) else Color.Transparent)
                            .border(2.dp, if (active) colors.primary else Color.Transparent, shape)
                            .padding(3.dp),
                    ) {
                        Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
                            for (row in 0 until 3) {
                                Row(Modifier.weight(1f), horizontalArrangement = Arrangement.spacedBy(2.dp)) {
                                    for (col in 0 until 3) {
                                        Cell(sharing, g, b * 9 + row * 3 + col, Modifier.weight(1f).fillMaxHeight(), 14.sp,
                                            win = false, faded = owner.isNotEmpty())
                                    }
                                }
                            }
                        }
                        if (owner == "x" || owner == "o") {
                            Text(
                                owner.uppercase(),
                                fontSize = 56.sp,
                                fontWeight = FontWeight.ExtraBold,
                                color = if (owner == "x") colors.primary else colors.tertiary,
                                modifier = Modifier.align(Alignment.Center),
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun RockPaperScissors(sharing: Sharing, g: GameView) {
    val rps = g.rps ?: return
    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(verticalAlignment = Alignment.Bottom, horizontalArrangement = Arrangement.spacedBy(16.dp)) {
            Text("You ${rps.score.first}", fontWeight = FontWeight.Bold, fontSize = 18.sp)
            Text("first to ${rps.target}", color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall)
            Text("${rps.score.second} ${g.opponent.name}", fontWeight = FontWeight.Bold, fontSize = 18.sp)
        }
        if (g.yourTurn) {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                for (choice in listOf("rock", "paper", "scissors")) {
                    OutlinedButton(onClick = { sharing.choose(g.id, choice) }, shape = RoundedCornerShape(12.dp)) {
                        Column(horizontalAlignment = Alignment.CenterHorizontally) {
                            Text(CHOICE_ICON.getValue(choice), fontSize = 30.sp)
                            Text(choiceName(choice))
                        }
                    }
                }
            }
        }
        for (round in rps.rounds.asReversed()) {
            val color = when (round.result) {
                "won" -> LocalAppStyle.current.ok
                "lost" -> MaterialTheme.colorScheme.error
                else -> MaterialTheme.colorScheme.onSurfaceVariant
            }
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(CHOICE_ICON.getValue(round.mine), fontSize = 22.sp)
                Text(
                    when (round.result) {
                        "won" -> "beats"
                        "lost" -> "loses to"
                        else -> "ties"
                    },
                    color = color,
                )
                Text(CHOICE_ICON.getValue(round.theirs), fontSize = 22.sp)
            }
        }
    }
}

/** "You 3 · boxes · 2 Them" above a board. */
@Composable
private fun ScoreLine(mine: String, middle: String, theirs: String) {
    Row(verticalAlignment = Alignment.Bottom, horizontalArrangement = Arrangement.spacedBy(14.dp)) {
        Text(mine, fontWeight = FontWeight.Bold, fontSize = 17.sp)
        if (middle.isNotEmpty()) {
            Text(middle, color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall)
        }
        Text(theirs, fontWeight = FontWeight.Bold, fontSize = 17.sp)
    }
}

@Composable
private fun FourInARowBoard(sharing: Sharing, g: GameView) {
    val hole = LocalAppStyle.current.solidSurface
    Column(
        Modifier.widthIn(max = 420.dp).fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(C4_FRAME).padding(8.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        for (row in 0 until C4_ROWS) {
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                for (col in 0 until C4_COLS) {
                    val i = row * C4_COLS + col
                    val playable = col in g.allowed
                    Box(
                        Modifier
                            .weight(1f)
                            .aspectRatio(1f)
                            .clickable(enabled = playable) { sharing.move(g.id, col) }
                            .padding(2.dp)
                            .clip(CircleShape)
                            .background(C4_DISC[g.board.getOrElse(i) { "" }] ?: hole)
                            .then(
                                when {
                                    i in g.line -> Modifier.border(4.dp, Color.White, CircleShape)
                                    g.last == i -> Modifier.border(3.dp, Color.White.copy(alpha = 0.8f), CircleShape)
                                    else -> Modifier
                                },
                            ),
                    )
                }
            }
        }
    }
}

@Composable
private fun CheckersBoard(sharing: Sharing, g: GameView) {
    // The squares picked so far: a piece, then where it goes (several landings for a chain of jumps).
    var picked by remember(g.id, g.paths) { mutableStateOf(emptyList<Int>()) }
    val chosen = if (g.yourTurn) picked else emptyList()
    val matching = g.paths.filter { p -> p.size > chosen.size && chosen.indices.all { p[it] == chosen[it] } }
    val starts = g.paths.map { it.first() }.toSet()
    val next = if (chosen.isEmpty()) emptySet() else matching.map { it[chosen.size] }.toSet()
    fun pick(square: Int) {
        if (square !in next) {
            if (square in starts) picked = listOf(square) // pick up a piece (or another one)
            return
        }
        val path = chosen + square
        val complete = g.paths.firstOrNull { it == path }
        picked = if (complete != null) emptyList() else path
        if (complete != null) sharing.movePath(g.id, complete)
    }
    val score = g.score ?: (0 to 0)
    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        ScoreLine("You ${score.first}", "pieces", "${score.second} ${g.opponent.name}")
        val edge = RoundedCornerShape(6.dp)
        Column(Modifier.widthIn(max = 400.dp).fillMaxWidth().aspectRatio(1f).border(3.dp, CHECKERS_EDGE, edge).padding(3.dp).clip(edge)) {
            for (shownRow in 0 until 8) {
                Row(Modifier.weight(1f)) {
                    for (shownCol in 0 until 8) {
                        val d = shownRow * 8 + shownCol
                        val square = if (g.seat == 1) 63 - d else d // your pieces are always at the bottom
                        val piece = g.board.getOrElse(square) { "" }
                        val ring = when {
                            chosen.lastOrNull() == square -> Color.White
                            square in next -> Color(0xFF36D36B)
                            square in g.lastPath -> Color(0x73FFFF00)
                            else -> null
                        }
                        Box(
                            Modifier
                                .weight(1f)
                                .fillMaxHeight()
                                .background(if ((square / 8 + square % 8) % 2 == 1) CHECKERS_DARK else CHECKERS_LIGHT)
                                .then(if (ring != null) Modifier.border(3.dp, ring) else Modifier)
                                .clickable(enabled = square in starts || square in next) { pick(square) },
                            contentAlignment = Alignment.Center,
                        ) {
                            if (piece.isNotEmpty()) {
                                val black = piece.lowercase() == "o"
                                Box(
                                    Modifier
                                        .fillMaxSize(0.76f)
                                        .clip(CircleShape)
                                        .background(if (black) Color(0xFF222222) else Color(0xFFD33A2C))
                                        .then(if (black) Modifier.border(2.dp, Color(0xFF777777), CircleShape) else Modifier),
                                    contentAlignment = Alignment.Center,
                                ) {
                                    if (piece == piece.uppercase()) Text("♛", color = Color(0xFFFFD84D), fontSize = 18.sp)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

/** The two dots a line joins, in dots across and down: horizontal lines first, then vertical ones. */
private fun dotsLine(line: Int): Pair<Offset, Offset> {
    val across = DOTS_SIZE * (DOTS_SIZE + 1)
    return if (line < across) {
        val r = (line / DOTS_SIZE).toFloat()
        val c = (line % DOTS_SIZE).toFloat()
        Offset(c, r) to Offset(c + 1, r)
    } else {
        val r = ((line - across) / (DOTS_SIZE + 1)).toFloat()
        val c = ((line - across) % (DOTS_SIZE + 1)).toFloat()
        Offset(c, r) to Offset(c, r + 1)
    }
}

@Composable
private fun DotsBoard(sharing: Sharing, g: GameView) {
    val mine = MaterialTheme.colorScheme.primary
    val theirs = LocalAppStyle.current.bolt
    val dot = MaterialTheme.colorScheme.onSurface
    val slot = MaterialTheme.colorScheme.outlineVariant
    val score = g.score ?: (0 to 0)
    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        ScoreLine("You ${score.first}", "boxes", "${score.second} ${g.opponent.name}")
        Canvas(
            Modifier
                .widthIn(max = 360.dp)
                .fillMaxWidth()
                .aspectRatio(1f)
                .pointerInput(g.id, g.allowed) {
                    // A tap draws the nearest open line, if it's close enough.
                    detectTapGestures { at ->
                        val pad = size.width * 0.05f
                        val gap = (size.width - 2 * pad) / DOTS_SIZE
                        val nearest = g.allowed.minByOrNull { line ->
                            val (a, b) = dotsLine(line)
                            hypot(pad + (a.x + b.x) / 2 * gap - at.x, pad + (a.y + b.y) / 2 * gap - at.y)
                        } ?: return@detectTapGestures
                        val (a, b) = dotsLine(nearest)
                        if (hypot(pad + (a.x + b.x) / 2 * gap - at.x, pad + (a.y + b.y) / 2 * gap - at.y) < gap * 0.6f) {
                            sharing.move(g.id, nearest)
                        }
                    }
                },
        ) {
            val pad = size.width * 0.05f
            val gap = (size.width - 2 * pad) / DOTS_SIZE
            fun at(p: Offset) = Offset(pad + p.x * gap, pad + p.y * gap)
            g.boxes.forEachIndexed { box, owner ->
                if (owner.isEmpty()) return@forEachIndexed
                val corner = at(Offset((box % DOTS_SIZE).toFloat(), (box / DOTS_SIZE).toFloat()))
                drawRoundRect(
                    (if (owner == g.mark) mine else theirs).copy(alpha = 0.32f),
                    corner + Offset(4f, 4f), Size(gap - 8f, gap - 8f), CornerRadius(gap / 10),
                )
            }
            val open = g.allowed.toSet()
            g.lines.forEachIndexed { line, owner ->
                val (a, b) = dotsLine(line)
                when {
                    owner.isNotEmpty() -> drawLine(
                        if (owner == g.mark) mine else theirs, at(a), at(b),
                        strokeWidth = gap * if (g.last == line) 0.14f else 0.1f, cap = StrokeCap.Round,
                    )
                    line in open -> drawLine(slot.copy(alpha = 0.6f), at(a), at(b), strokeWidth = gap * 0.06f, cap = StrokeCap.Round)
                }
            }
            for (r in 0..DOTS_SIZE) for (c in 0..DOTS_SIZE) drawCircle(dot, gap * 0.08f, at(Offset(c.toFloat(), r.toFloat())))
        }
    }
}

@Composable
private fun SeaGrid(label: String, ships: Set<Int>, marks: Map<Int, String>, mine: Boolean, onFire: ((Int) -> Unit)?) {
    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(label, fontWeight = FontWeight.SemiBold, style = MaterialTheme.typography.labelLarge)
        Column(
            Modifier.widthIn(max = 300.dp).fillMaxWidth().aspectRatio(1f).clip(RoundedCornerShape(8.dp)).background(SEA).padding(4.dp),
            verticalArrangement = Arrangement.spacedBy(2.dp),
        ) {
            for (r in 0 until GRID) {
                Row(Modifier.weight(1f), horizontalArrangement = Arrangement.spacedBy(2.dp)) {
                    for (c in 0 until GRID) {
                        val cell = r * GRID + c
                        val mark = marks[cell]
                        Box(
                            Modifier
                                .weight(1f)
                                .fillMaxHeight()
                                .clip(RoundedCornerShape(3.dp))
                                .background(
                                    when {
                                        mark == "sunk" && !mine -> SUNK
                                        mark == "hit" || mark == "sunk" -> HIT
                                        cell in ships -> SHIP
                                        else -> Color.White.copy(alpha = 0.14f)
                                    },
                                )
                                .clickable(enabled = onFire != null && mark == null) { onFire?.invoke(cell) },
                            contentAlignment = Alignment.Center,
                        ) {
                            if (mark == "miss" || mark == "pending") {
                                Box(Modifier.fillMaxSize(0.32f).clip(CircleShape).background(SPLASH.copy(alpha = if (mark == "miss") 1f else 0.5f)))
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun BattleshipBoard(sharing: Sharing, g: GameView) {
    val b = g.battle ?: return
    val myShips = b.ships.flatten().toSet()
    val incoming = b.incoming.associate { it.cell to (it.result ?: "pending") }
    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
        if (b.phase == "setup") {
            SeaGrid("Your fleet", myShips, incoming, mine = true, onFire = null)
            if (!b.ready) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { sharing.shuffleFleet(g.id) }) { Text("Shuffle ships") }
                    ThemedButton(onClick = { sharing.fleetReady(g.id) }) { Text("Ready") }
                }
            }
            Note(if (b.theyReady) "${g.opponent.name} is ready." else "${g.opponent.name} is placing ships…")
            return@Column
        }
        val shots = b.outgoing.associate { it.cell to (it.result ?: "pending") }
        SeaGrid("${g.opponent.name}’s waters", b.theirShips.flatten().toSet(), shots, mine = false,
            onFire = if (g.yourTurn) ({ cell -> sharing.fire(g.id, cell) }) else null)
        SeaGrid("Your fleet", myShips, incoming, mine = true, onFire = null)
        Note("Ships sunk: ${b.sunk.first} of ${FLEET.size} of theirs, ${b.sunk.second} of ${FLEET.size} of yours.")
    }
}

@Composable
private fun MemoryBoard(sharing: Sharing, g: GameView) {
    val colors = MaterialTheme.colorScheme
    val bolt = LocalAppStyle.current.bolt
    val score = g.score ?: (0 to 0)
    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        ScoreLine("You ${score.first}", "pairs", "${score.second} ${g.opponent.name}")
        Column(Modifier.widthIn(max = 340.dp).fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            for (r in 0 until 4) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    for (c in 0 until 4) {
                        val i = r * 4 + c
                        val picture = g.cards.getOrNull(i)
                        val owner = g.matched.getOrElse(i) { "" }
                        val shape = RoundedCornerShape(10.dp)
                        val (ringWidth, ring) = when {
                            owner.isNotEmpty() -> 3.dp to if (owner == g.mark) colors.primary else bolt
                            i in g.shown -> 3.dp to colors.error
                            else -> 1.dp to colors.outlineVariant
                        }
                        Box(
                            Modifier
                                .weight(1f)
                                .aspectRatio(1f)
                                .alpha(if (owner.isNotEmpty()) 0.75f else 1f)
                                .clip(shape)
                                .background(if (picture != null) colors.surfaceContainerHighest else colors.primary)
                                .border(ringWidth, ring, shape)
                                .clickable(enabled = picture == null && i in g.allowed) { sharing.move(g.id, i) },
                            contentAlignment = Alignment.Center,
                        ) {
                            Text(
                                picture?.let { CARD_PICTURES.getOrElse(it) { "?" } } ?: "?",
                                fontSize = 30.sp,
                                fontWeight = FontWeight.Bold,
                                color = if (picture != null) colors.onSurface else colors.onPrimary,
                            )
                        }
                    }
                }
            }
        }
    }
}

// ----- typing race and math sprint (timed here, on this device) ----------------------------------------

/** 3, 2, 1, then [go]: a big countdown before the clock starts. */
@Composable
private fun Countdown(go: () -> Unit) {
    var n by remember { mutableIntStateOf(3) }
    LaunchedEffect(Unit) {
        while (n > 1) {
            delay(700)
            n--
        }
        delay(700)
        go()
    }
    Text("$n", fontSize = 64.sp, fontWeight = FontWeight.ExtraBold, color = MaterialTheme.colorScheme.primary)
}

@Composable
private fun SentenceBox(text: AnnotatedString) {
    Text(
        text,
        fontSize = 18.sp,
        lineHeight = 28.sp,
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surfaceContainerHigh)
            .border(1.dp, MaterialTheme.colorScheme.outlineVariant, RoundedCornerShape(10.dp))
            .padding(horizontal = 12.dp, vertical = 10.dp),
    )
}

// Where a timed game is: a Start button, the countdown, then the clock running.
private const val NOT_STARTED = 0
private const val COUNTING = 1
private const val GOING = 2

@Composable
private fun TypingRace(sharing: Sharing, g: GameView) {
    val race = g.race ?: return
    if (race.myTime != null || g.status != GAME_PLAYING) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(10.dp)) {
            SentenceBox(AnnotatedString(race.sentence))
            ScoreLine(
                "You " + (race.myTime?.let(::seconds) ?: "—"), "",
                g.opponent.name + " " + (race.theirTime?.let(::seconds) ?: if (race.theyDone) "finished" else "…"),
            )
        }
        return
    }
    var stage by remember(g.id) { mutableIntStateOf(NOT_STARTED) }
    var typed by remember(g.id) { mutableStateOf("") }
    var started by remember(g.id) { mutableLongStateOf(0L) }
    var clock by remember(g.id) { mutableLongStateOf(0L) }
    var done by remember(g.id) { mutableStateOf(false) }
    val focus = remember { FocusRequester() }
    Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(10.dp)) {
        when (stage) {
            NOT_STARTED -> ThemedButton(onClick = { stage = COUNTING }) { Text("Start") }
            COUNTING -> Countdown {
                started = SystemClock.elapsedRealtime()
                stage = GOING
            }
            else -> {
                LaunchedEffect(Unit) { focus.requestFocus() }
                LaunchedEffect(done) {
                    while (!done) {
                        clock = SystemClock.elapsedRealtime() - started
                        delay(50)
                    }
                }
                val ok = LocalAppStyle.current.ok
                val bad = MaterialTheme.colorScheme.error
                SentenceBox(
                    buildAnnotatedString {
                        race.sentence.forEachIndexed { i, ch ->
                            when {
                                i >= typed.length -> append(ch)
                                typed[i] == ch -> withStyle(SpanStyle(color = ok)) { append(ch) }
                                else -> withStyle(SpanStyle(color = bad, background = bad.copy(alpha = 0.15f))) { append(ch) }
                            }
                        }
                    },
                )
                OutlinedTextField(
                    value = typed,
                    onValueChange = { value ->
                        if (done) return@OutlinedTextField
                        typed = value
                        if (value == race.sentence) {
                            done = true
                            val ms = (SystemClock.elapsedRealtime() - started).toInt()
                            clock = ms.toLong()
                            sharing.finish(g.id, ms.coerceIn(300, 600_000))
                        }
                    },
                    enabled = !done,
                    minLines = 3,
                    keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.None, autoCorrectEnabled = false),
                    modifier = Modifier.fillMaxWidth().focusRequester(focus),
                )
                Text(seconds(clock.toInt()), fontWeight = FontWeight.Bold, fontSize = 20.sp)
            }
        }
    }
}

@Composable
private fun MathSprint(sharing: Sharing, g: GameView) {
    val sprint = g.sprint ?: return
    val total = sprint.problems.size
    var answered by remember(g.id) { mutableIntStateOf(0) } // sums answered here (the view can lag a little)
    val k = maxOf(answered, sprint.myTimes.size)
    if (k >= total || g.status != GAME_PLAYING) {
        val ok = LocalAppStyle.current.ok
        val bad = MaterialTheme.colorScheme.error
        Column(Modifier.widthIn(max = 420.dp).fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            sprint.problems.forEachIndexed { i, (question, answer) ->
                val mine = sprint.myTimes.getOrNull(i)
                val theirs = sprint.theirTimes.getOrNull(i)
                val color = when {
                    mine == null || theirs == null -> MaterialTheme.colorScheme.onSurface
                    mine < theirs -> ok
                    mine > theirs -> bad
                    else -> MaterialTheme.colorScheme.onSurface
                }
                Row {
                    Text("${i + 1}. $question = $answer", color = color, modifier = Modifier.weight(1f))
                    Text(
                        (mine?.let(::seconds) ?: "—") + " vs " + (theirs?.let(::seconds) ?: "…"),
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        }
        return
    }
    var stage by remember(g.id) { mutableIntStateOf(NOT_STARTED) }
    var typed by remember(g.id) { mutableStateOf("") }
    var shownAt by remember(g.id) { mutableLongStateOf(0L) }
    val focus = remember { FocusRequester() }
    Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(10.dp)) {
        when (stage) {
            NOT_STARTED -> ThemedButton(onClick = { stage = COUNTING }) { Text("Start") }
            COUNTING -> Countdown {
                shownAt = SystemClock.elapsedRealtime()
                stage = GOING
            }
            else -> {
                LaunchedEffect(Unit) { focus.requestFocus() }
                val (question, answer) = sprint.problems[k]
                Note("Sum ${k + 1} of $total")
                Text("$question = ?", fontSize = 40.sp, fontWeight = FontWeight.ExtraBold)
                OutlinedTextField(
                    value = typed,
                    onValueChange = { value ->
                        val digits = value.filter { it.isDigit() }.take(6)
                        typed = digits
                        if (digits.toIntOrNull() == answer) {
                            val ms = (SystemClock.elapsedRealtime() - shownAt).toInt().coerceIn(100, 600_000)
                            sharing.finish(g.id, ms)
                            answered = k + 1
                            typed = ""
                            shownAt = SystemClock.elapsedRealtime()
                        }
                    },
                    singleLine = true,
                    textStyle = MaterialTheme.typography.headlineMedium.copy(textAlign = TextAlign.Center),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.width(180.dp).focusRequester(focus),
                )
                Note("${g.opponent.name}: ${sprint.theirCount} of $total")
            }
        }
    }
}

// ----- starting a game -----------------------------------------------------------------------------

@Composable
private fun KindCard(sharing: Sharing, kind: String, opponents: List<Opponent>, online: Boolean, onOpen: (String) -> Unit) {
    var chosen by rememberSaveable(kind) { mutableStateOf<String?>(null) }
    var bestOf by rememberSaveable(kind) { mutableIntStateOf(3) }
    val peer = opponents.firstOrNull { it.peerId == chosen } ?: opponents.firstOrNull()
    PlayCard {
        Text(GAME_TITLES.getValue(kind), fontWeight = FontWeight.SemiBold)
        Text(ABOUT.getValue(kind), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (opponents.isNotEmpty()) {
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                for (o in opponents) {
                    FilterChip(selected = o.peerId == peer?.peerId, onClick = { chosen = o.peerId }, label = { Text(o.name) })
                }
            }
        }
        if (kind == RPS) {
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                for (n in BEST_OF) FilterChip(selected = n == bestOf, onClick = { bestOf = n }, label = { Text("Best of $n") })
            }
        }
        ThemedButton(
            onClick = { peer?.let { sharing.invite(it.peerId, kind, bestOf) { game -> onOpen(game.id) } } },
            enabled = peer != null && online,
        ) { Text(peer?.let { "Challenge ${it.name}" } ?: "Challenge") }
    }
}

