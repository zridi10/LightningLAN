package lightninglan.app.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.boundsInRoot
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt

/*
 * The tutorial: a guided tour of the main screen, like the desktop app's (static/tutorial.js).
 * It plays by itself once, after the first setup; Settings can show it again. Each step dims the
 * screen, puts a spotlight on one button and explains it in a card. Next moves on and Back goes
 * back; a step with a hint asks for a tap on the spotlighted button itself (the two tabs). Skip,
 * or the back gesture, ends it. Nothing else on the screen reacts meanwhile, and nothing has to be
 * shared or played.
 */

private class TutorialStep(
    /** What it points at (see [tutorialTarget]); none, or off screen: the card in the middle. */
    val target: String?,
    val title: String,
    val text: String,
    /** The tab it is on. */
    val tab: String = "files",
    /** The step asks for a tap on its target, which opens this tab. */
    val tapOpens: String? = null,
    val hint: String? = null,
)

private val STEPS = listOf(
    TutorialStep(
        null, "Welcome to LightningLAN ⚡",
        "Share files with everyone on this Wi-Fi, phones and computers, and play games while they download. There's " +
            "nothing to set up. This one-minute tour shows you the main buttons; you won't have to share or play anything.",
    ),
    TutorialStep(
        "status", "This is you",
        "Everyone on the network sees this phone by this name (change it in Settings). Stop takes the phone off the " +
            "network until you start it again.",
    ),
    TutorialStep(
        "devices", "Who's here",
        "How many other phones and computers running LightningLAN are on this Wi-Fi. They show up by themselves; tap it " +
            "to see them.",
    ),
    TutorialStep(
        "share", "Share files",
        "Pick files on the phone to share them. Everything in your LightningLAN Shared folder is shared too, and so is " +
            "what you send to LightningLAN from other apps' Share menu.",
    ),
    TutorialStep(
        "search", "ThunderCLOUD",
        "Every file everyone shares, in one list below. Search, sort or pick who shares it, and tap Get next to a file " +
            "to download it.",
    ),
    TutorialStep(
        "phones", "Other phones",
        "Tap this for a QR code: a phone without the app, an iPhone say, scans it and uses LightningLAN in its browser. " +
            "No Wi-Fi in common? This phone can make a hotspot there too.",
    ),
    TutorialStep(
        "playTab", "While you wait", "Download taking a while? Play games against the others on the network.",
        tapOpens = "play", hint = "Tap While you wait to have a look.",
    ),
    TutorialStep(
        "games", "Games",
        "Every game has a card like this one: pick who to play and challenge them, and they get an invitation. Solo, " +
            "at the top, has Falling blocks to play on your own.",
        tab = "play",
    ),
    TutorialStep(
        "filesTab", "Back to your files", "Invitations and games waiting for you show a badge on While you wait.",
        tab = "play", tapOpens = "files", hint = "Tap Files to go back.",
    ),
    TutorialStep(
        "settings", "Settings",
        "Your name, folders, theme and an optional passphrase are in Settings, and so is this tutorial if you want to " +
            "see it again.",
    ),
    TutorialStep(null, "You're all set", "That's all there is to it. Have fun at the LAN party!"),
)

/** Which step shows (none: -1), and where the buttons it points at are on the screen. */
internal class Tutorial {
    var step by mutableIntStateOf(-1)
        private set
    val targets = mutableStateMapOf<String, Rect>()

    fun start() {
        step = 0
    }

    fun next() {
        step = if (step + 1 < STEPS.size) step + 1 else -1
    }

    fun back() {
        if (step > 0) step--
    }

    fun end() {
        step = -1
    }
}

internal val LocalTutorial = staticCompositionLocalOf<Tutorial?> { null }

/** Marks something a tutorial step points at, under [key]. */
internal fun Modifier.tutorialTarget(key: String): Modifier = composed {
    val tutorial = LocalTutorial.current ?: return@composed Modifier
    DisposableEffect(tutorial, key) { onDispose { tutorial.targets.remove(key) } }
    Modifier.onGloballyPositioned { coordinates ->
        val bounds = coordinates.boundsInRoot()
        if (tutorial.targets[key] != bounds) tutorial.targets[key] = bounds
    }
}

/** The tour over the main screen, while it runs. [showTab] switches the main screen's tab. */
@Composable
internal fun TutorialOverlay(tutorial: Tutorial, showTab: (String) -> Unit) {
    val index = tutorial.step
    if (index < 0) return
    val step = STEPS[index]
    val last = index == STEPS.lastIndex
    fun end() {
        tutorial.end()
        showTab("files")
    }
    LaunchedEffect(index) { showTab(step.tab) }
    BackHandler { end() }
    val density = LocalDensity.current
    val hole = step.target?.let { tutorial.targets[it] }?.inflate(with(density) { 6.dp.toPx() })
    val tapping = step.tapOpens != null && hole != null
    val accent = MaterialTheme.colorScheme.primary
    val muted = MaterialTheme.colorScheme.onSurfaceVariant

    BoxWithConstraints(
        Modifier.fillMaxSize().pointerInput(index, hole) {
            // Swallows every touch; on a step that asks for it, a tap on the spotlighted button does what it would.
            detectTapGestures { at ->
                if (tapping && hole.contains(at)) {
                    step.tapOpens?.let(showTab)
                    tutorial.next()
                }
            }
        },
    ) {
        Canvas(Modifier.fillMaxSize().graphicsLayer(compositingStrategy = CompositingStrategy.Offscreen)) {
            drawRect(Color(0xA6060812))
            if (hole != null) {
                val corner = CornerRadius(12.dp.toPx())
                drawRoundRect(Color.Black, hole.topLeft, hole.size, corner, blendMode = BlendMode.Clear)
                if (tapping) drawRoundRect(accent, hole.topLeft, hole.size, corner, style = Stroke(3.dp.toPx()))
            }
        }
        // The card goes below the spotlight if there's room, else above it.
        var cardHeight by remember { mutableIntStateOf(0) }
        val height = constraints.maxHeight.toFloat()
        val margin = with(density) { 16.dp.toPx() }
        val gap = with(density) { 12.dp.toPx() }
        val y = when {
            hole == null -> (height - cardHeight) / 2
            hole.bottom + gap + cardHeight <= height - margin -> hole.bottom + gap
            hole.top - gap - cardHeight >= margin -> hole.top - gap - cardHeight
            else -> height - cardHeight - margin
        }
        Surface(
            modifier = Modifier.align(Alignment.TopCenter).offset { IntOffset(0, y.roundToInt()) }
                .padding(horizontal = 16.dp).widthIn(max = 440.dp).fillMaxWidth().onSizeChanged { cardHeight = it.height }
                .semantics { liveRegion = LiveRegionMode.Polite },
            shape = RoundedCornerShape(16.dp),
            color = LocalAppStyle.current.solidSurface,
            shadowElevation = 12.dp,
        ) {
            Column(Modifier.padding(start = 18.dp, top = 16.dp, end = 12.dp, bottom = 6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                Text("${index + 1} of ${STEPS.size}", style = MaterialTheme.typography.labelMedium, color = muted)
                Text(step.title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                Text(step.text, style = MaterialTheme.typography.bodyMedium)
                if (tapping) Text(step.hint ?: "", color = accent, fontWeight = FontWeight.Medium)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    if (!last) TextButton(onClick = ::end) { Text("Skip tutorial", color = muted) }
                    Spacer(Modifier.weight(1f))
                    if (index > 0) TextButton(onClick = tutorial::back) { Text("Back") }
                    if (!tapping) {
                        ThemedButton(onClick = { if (last) end() else tutorial.next() }) {
                            Text(if (index == 0) "Show me around" else if (last) "Finish" else "Next")
                        }
                    }
                }
            }
        }
    }
}
