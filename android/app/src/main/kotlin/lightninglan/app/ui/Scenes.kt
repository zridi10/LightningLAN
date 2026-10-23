package lightninglan.app.ui

import android.provider.Settings
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.State
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.geometry.RoundRect
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Matrix
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.clipPath
import androidx.compose.ui.graphics.drawscope.clipRect
import androidx.compose.ui.graphics.drawscope.withTransform
import androidx.compose.ui.graphics.vector.PathParser
import androidx.compose.ui.platform.LocalContext
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow

/*
 * The animated scenes behind the Synthwave, Bliss and Deep Sea themes: the desktop app's
 * (static/style.css and the pictures in static/themes, as its phone page shows them), drawn with Compose.
 * Sizes follow the CSS, with 1 CSS pixel = 1 dp. With animations turned off in Android's
 * settings the scene stands still.
 */

@Composable
fun ThemeScene(style: AppStyle) {
    if (!style.scene) return
    val time = sceneTime()
    when (style.theme) {
        AppTheme.SYNTHWAVE -> SynthwaveScene(time)
        AppTheme.BLISS -> BlissScene(time)
        AppTheme.DEEPSEA -> DeepSeaScene(time)
        else -> Unit
    }
}

/** Seconds since the scene appeared; only the drawing reads it, so nothing else recomposes. */
@Composable
private fun sceneTime(): State<Float> {
    val context = LocalContext.current
    val animate = remember {
        Settings.Global.getFloat(context.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) > 0f
    }
    val time = remember { mutableFloatStateOf(0f) }
    if (animate) {
        LaunchedEffect(Unit) {
            val start = withFrameNanos { it }
            while (true) withFrameNanos { time.floatValue = (it - start) / 1e9f }
        }
    }
    return time
}

// ----- small helpers ---------------------------------------------------------------------------

private fun svgPath(d: String): Path = PathParser().parsePathString(d).toPath()

private fun frac(x: Float): Float = x - floor(x)

/** CSS "alternate": 0 to 1 and back again, once each [period] seconds. */
private fun alternate(t: Float, period: Float): Float {
    val p = frac(t / (2 * period)) * 2
    return if (p < 1) p else 2 - p
}

/** Roughly CSS ease-in-out. */
private fun ease(p: Float): Float = p * p * (3 - 2 * p)

private fun lerp(a: Float, b: Float, p: Float): Float = a + (b - a) * p

private fun lerp(a: Color, b: Color, p: Float): Color =
    Color(lerp(a.red, b.red, p), lerp(a.green, b.green, p), lerp(a.blue, b.blue, p), lerp(a.alpha, b.alpha, p))

/** Piecewise-linear keyframes: (progress, value) pairs from 0 to 1. */
private fun keyframes(p: Float, frames: List<Pair<Float, Float>>): Float {
    for (i in 1 until frames.size) {
        val (p1, v1) = frames[i]
        val (p0, v0) = frames[i - 1]
        if (p <= p1) return lerp(v0, v1, if (p1 > p0) (p - p0) / (p1 - p0) else 1f)
    }
    return frames.last().second
}

/** A copy of an SVG path stretched from its viewBox onto a box on screen (preserveAspectRatio="none"). */
private fun stretched(src: Path, viewW: Float, viewH: Float, box: Rect): Path = Path().apply {
    addPath(src)
    transform(Matrix(floatArrayOf(box.width / viewW, 0f, 0f, 0f, 0f, box.height / viewH, 0f, 0f, 0f, 0f, 1f, 0f, box.left, box.top, 0f, 1f)))
}

/** Draws in an SVG's own coordinates, scaled uniformly into a box at ([left], [top]). */
private inline fun DrawScope.inViewBox(left: Float, top: Float, scale: Float, block: DrawScope.() -> Unit) =
    withTransform({
        translate(left, top)
        scale(scale, scale, Offset.Zero)
    }, block)

private fun vertical(bounds: Rect, vararg stops: Pair<Float, Color>): Brush =
    Brush.verticalGradient(*stops, startY = bounds.top, endY = bounds.bottom)

// ----- Synthwave --------------------------------------------------------------------------------

private class Star(val x: Float, val y: Float, val size: Float, val color: Color)

private val WHITE = Color.White
private val PINKISH = Color(0xFFFFD6F5)
private val MINT = Color(0xFFC9FFF4)

private val STARS_A = listOf(
    Star(7f, 14f, 2f, WHITE), Star(18f, 32f, 2f, WHITE), Star(27f, 8f, 1.5f, WHITE), Star(39f, 24f, 2f, PINKISH),
    Star(52f, 11f, 1.5f, WHITE), Star(61f, 36f, 2f, WHITE), Star(73f, 18f, 1.5f, MINT), Star(84f, 7f, 2f, WHITE),
    Star(92f, 29f, 1.5f, WHITE), Star(46f, 42f, 2f, WHITE), Star(13f, 46f, 1.5f, WHITE), Star(88f, 48f, 2f, PINKISH),
)
private val STARS_B = listOf(
    Star(4f, 30f, 1.5f, WHITE), Star(22f, 20f, 2f, MINT), Star(33f, 40f, 1.5f, WHITE), Star(44f, 6f, 2f, WHITE),
    Star(57f, 27f, 1.5f, WHITE), Star(68f, 9f, 2f, PINKISH), Star(79f, 33f, 1.5f, WHITE), Star(95f, 15f, 2f, WHITE),
    Star(65f, 45f, 1.5f, WHITE), Star(30f, 52f, 2f, WHITE), Star(10f, 4f, 1.5f, WHITE), Star(76f, 52f, 2f, MINT),
)

// The sun's stripes are gaps: it is drawn in these bands only (viewBox 200).
private val SUN_BANDS = listOf(0f to 106f, 109f to 121f, 126f to 138f, 145f to 157f, 166f to 177f, 188f to 200f)

@Composable
private fun SynthwaveScene(time: State<Float>) {
    val mountains = remember {
        svgPath(
            "M0 200 L0 120 L70 80 L130 115 L210 50 L290 105 L360 85 L440 135 L520 160 L600 172 L680 160 " +
                "L760 135 L840 90 L910 110 L990 45 L1070 100 L1130 75 L1200 115 L1200 200 Z",
        )
    }
    val ridges = remember { svgPath("M210 50 L250 120 M990 45 L950 125 M70 80 L95 140 M1130 75 L1105 135") }
    Spacer(
        Modifier.fillMaxSize().drawWithCache {
            val w = size.width
            val h = size.height
            val dp = density
            val sky = Brush.verticalGradient(
                0f to Color(0xFF090118), 0.32f to Color(0xFF1D0B40), 0.52f to Color(0xFF4A1470),
                0.635f to Color(0xFFC0266F), 0.64f to Color(0xFF12052A),
            )
            val mountainBox = Rect(0f, 0.51f * h, w, 0.64f * h)
            val rock = stretched(mountains, 1200f, 200f, mountainBox)
            val rockFill = vertical(rock.getBounds(), 0f to Color(0xFF3A1370), 1f to Color(0xFF12052A))
            val ridgeLines = stretched(ridges, 1200f, 200f, mountainBox)
            val horizon = 0.64f * h
            val floorHeight = h - horizon
            val floor = Brush.verticalGradient(listOf(Color(0xFF2D0B52), Color(0xFF0B0220)), startY = horizon, endY = h)
            val floorGlow = Brush.verticalGradient(
                listOf(Color(0x99C0266F), Color.Transparent), startY = horizon, endY = horizon + 0.4f * floorHeight,
            )
            val sunSize = min(0.4f * w, 400 * dp)
            val sunLeft = (w - sunSize) / 2
            val sunTop = 0.69f * h - sunSize
            val sunFill = Brush.verticalGradient(
                0f to Color(0xFFFFF176), 0.45f to Color(0xFFFF9D3C), 1f to Color(0xFFFF2F8F), startY = 4f, endY = 196f,
            )
            onDrawBehind {
                val t = time.value
                drawRect(sky)
                // Stars, in two layers that twinkle out of step.
                val starsHeight = 0.58f * h
                val a = 0.35f + 0.65f * ease(alternate(t, 2.6f))
                val b = 0.35f + 0.65f * ease(1 - alternate(t, 3.4f))
                for ((stars, alpha) in listOf(STARS_A to a, STARS_B to b)) {
                    for (s in stars) {
                        drawCircle(s.color, radius = s.size * 0.75f * dp, center = Offset(s.x / 100 * w, s.y / 100 * starsHeight), alpha = alpha)
                    }
                }
                // The sun, glowing pink and then orange.
                val glow = ease(alternate(t, 3.5f))
                val blur = lerp(28f, 48f, glow) * dp
                val glowColor = lerp(Color(0xB3FF40A0), Color(0xD9FF8C3C), glow)
                val center = Offset(sunLeft + sunSize / 2, sunTop + sunSize / 2)
                val radius = sunSize * 0.48f
                val outer = radius + 2 * blur
                drawCircle(
                    Brush.radialGradient(
                        radius / outer to glowColor, (radius + blur) / outer to glowColor.copy(alpha = glowColor.alpha * 0.3f),
                        1f to Color.Transparent, center = center, radius = outer,
                    ),
                    radius = outer, center = center,
                )
                inViewBox(sunLeft, sunTop, sunSize / 200f) {
                    for ((top, bottom) in SUN_BANDS) {
                        clipRect(0f, top, 200f, bottom) { drawCircle(sunFill, radius = 96f, center = Offset(100f, 100f)) }
                    }
                }
                // Mountains with neon edges.
                drawPath(rock, rockFill)
                drawPath(rock, Color(0xFFFF2FB3), style = Stroke(2.5f * dp, join = StrokeJoin.Round))
                drawPath(ridgeLines, Color(0xFF36F9C6), alpha = 0.55f, style = Stroke(1.5f * dp))
                // The floor and its grid, rolling towards you.
                drawRect(floor, topLeft = Offset(0f, horizon), size = Size(w, floorHeight))
                clipRect(0f, horizon, w, h) { drawGrid(t, horizon, floorHeight) }
                drawRect(floorGlow, topLeft = Offset(0f, horizon), size = Size(w, 0.4f * floorHeight))
            }
        },
    )
}

/** A perspective grid on the floor: rows come closer one spacing every 1.1 s. */
private fun DrawScope.drawGrid(t: Float, horizon: Float, floorHeight: Float) {
    val dp = density
    val spacing = 0.2f // in units where the bottom edge of the screen is at depth 1
    val far = 14f
    val centerX = size.width / 2
    val phase = frac(t / 1.1f)
    val pink = Color(0xFFFF3FBF)
    var i = 0
    while (true) {
        val z = 1 + (i - phase) * spacing
        i++
        if (z <= 0.5f) continue
        if (z > far) break
        val y = horizon + floorHeight / z
        val fade = min(1f, (far - z) / (far * 0.5f))
        drawLine(pink, Offset(0f, y), Offset(size.width, y), strokeWidth = max(0.6f, 2f / z.pow(0.7f)) * dp, alpha = 0.85f * fade)
    }
    val cyan = Color(0xFF36F9C6)
    val lines = (centerX / (floorHeight * spacing)).toInt() + 3
    for (j in -lines..lines) {
        val x = j * spacing * floorHeight
        drawLine(
            cyan, Offset(centerX + x / far, horizon + floorHeight / far), Offset(centerX + x, horizon + floorHeight),
            strokeWidth = 2f * dp, alpha = 0.55f,
        )
    }
}

// ----- Bliss ------------------------------------------------------------------------------------

private class Cloud(val top: Float, val width: Float, val duration: Float, val delay: Float, val alpha: Float)

// The phone page's three clouds: top (vh), width (dp), seconds to cross, head start (s), opacity.
private val CLOUDS = listOf(Cloud(6f, 220f, 95f, 12f, 1f), Cloud(21f, 300f, 130f, 70f, 1f), Cloud(13f, 150f, 75f, 40f, 0.85f))

@Composable
private fun BlissScene(time: State<Float>) {
    val far = remember { svgPath("M0 230 C 260 120 520 110 760 190 C 980 260 1220 150 1440 170 L1440 400 L0 400 Z") }
    val near = remember { svgPath("M0 320 C 300 190 640 210 920 290 C 1110 340 1300 300 1440 268 L1440 400 L0 400 Z") }
    val crest = remember { svgPath("M0 320 C 300 190 640 210 920 290") }
    val ray = remember { svgPath("M100 6 L109 42 L91 42 Z") }
    val cloud = remember {
        Path().apply {
            addOval(Rect(Offset(70f, 72f), 34f))
            addOval(Rect(Offset(112f, 54f), 42f))
            addOval(Rect(Offset(160f, 66f), 34f))
            addOval(Rect(Offset(194f, 82f), 24f))
            addRoundRect(RoundRect(38f, 72f, 216f, 108f, CornerRadius(18f)))
        }
    }
    Spacer(
        Modifier.fillMaxSize().drawWithCache {
            val w = size.width
            val h = size.height
            val dp = density
            val sky = Brush.verticalGradient(
                0f to Color(0xFF1C64D8), 0.32f to Color(0xFF4D9CF0), 0.62f to Color(0xFF98CFFB), 0.8f to Color(0xFFD9F1FF),
            )
            val hillsBox = Rect(0f, 0.56f * h, w, h)
            val farHill = stretched(far, 1440f, 400f, hillsBox)
            val nearHill = stretched(near, 1440f, 400f, hillsBox)
            val crestLine = stretched(crest, 1440f, 400f, hillsBox)
            val farFill = vertical(farHill.getBounds(), 0f to Color(0xFF8FD65A), 1f to Color(0xFF3F9A2A))
            val nearFill = vertical(nearHill.getBounds(), 0f to Color(0xFF74C94A), 1f to Color(0xFF2A7A1E))
            val puff = Brush.verticalGradient(0f to Color.White, 0.7f to Color(0xFFF4F9FF), 1f to Color(0xFFD6E6F5), startY = 14f, endY = 110f)
            val sunSize = (0.15f * w).coerceIn(110 * dp, 190 * dp)
            val sunLeft = w - 0.07f * w - sunSize
            val sunTop = 0.05f * h
            val core = Brush.radialGradient(
                0f to Color(0xFFFFFBE0), 0.55f to Color(0xFFFFDC4A), 1f to Color(0xFFFFB000),
                center = Offset(92.6f, 89f), radius = 59.8f,
            )
            onDrawBehind {
                val t = time.value
                drawRect(sky)
                // The sun, turning slowly.
                inViewBox(sunLeft, sunTop, sunSize / 200f) {
                    withTransform({ rotate(t / 80f * 360f, Offset(100f, 100f)) }) {
                        for (k in 0 until 12) {
                            withTransform({ rotate(k * 30f, Offset(100f, 100f)) }) { drawPath(ray, Color(0xFFFFE066), alpha = 0.8f) }
                        }
                    }
                    drawCircle(Color(0xFFFFF3A6), radius = 58f, center = Offset(100f, 100f), alpha = 0.35f)
                    drawCircle(core, radius = 46f, center = Offset(100f, 100f))
                }
                // Clouds drifting across.
                for (c in CLOUDS) {
                    val x = lerp(-340 * dp, w + 40 * dp, frac((t + c.delay) / c.duration))
                    inViewBox(x, c.top / 100 * h, c.width * dp / 240f) { drawPath(cloud, puff, alpha = c.alpha) }
                }
                // Rolling green hills.
                drawPath(farHill, farFill)
                drawPath(nearHill, nearFill)
                drawPath(crestLine, Color(0xFFC4F09A), alpha = 0.45f, style = Stroke(5 * dp))
            }
        },
    )
}

// ----- Deep Sea ---------------------------------------------------------------------------------

private class Bubble(val left: Float, val size: Float, val duration: Float, val delay: Float)

// The phone page's four bubbles: left (%), size (dp), seconds to rise, head start (s).
private val BUBBLES = listOf(Bubble(6f, 12f, 12f, 0f), Bubble(22f, 16f, 15f, 9f), Bubble(58f, 12f, 13f, 7f), Bubble(84f, 14f, 14f, 11f))

/** deepsea-clownfish.svg (viewBox 120 x 70). */
private class Clownfish {
    val tail = svgPath("M26 36 L5 16 Q0 36 5 56 Z")
    val dorsal = svgPath("M48 16 Q60 1 78 13 Z")
    val belly = svgPath("M60 48 Q66 63 74 50 Z")
    val body = Path().apply { addOval(Rect(20f, 13f, 104f, 59f)) }
    val mouth = svgPath("M104 40 Q100 44 95 42")

    fun draw(scope: DrawScope) = with(scope) {
        val orange = Color(0xFFFF7A1A)
        val ink = Color(0xFF1B1B1B)
        for (fin in listOf(tail, dorsal)) {
            drawPath(fin, orange)
            drawPath(fin, ink, style = Stroke(2.5f, join = StrokeJoin.Round))
        }
        drawPath(body, orange)
        clipPath(body) {
            for ((x, width) in listOf(30f to 9f, 56f to 11f, 83f to 8f)) {
                drawRect(Color.White, Offset(x, 0f), Size(width, 70f))
                drawRect(ink, Offset(x, 0f), Size(width, 70f), style = Stroke(2.5f))
            }
        }
        drawPath(body, ink, style = Stroke(2.5f))
        drawPath(belly, orange)
        drawPath(belly, ink, style = Stroke(2f, join = StrokeJoin.Round))
        drawCircle(Color.White, 5f, Offset(97f, 30f))
        drawCircle(ink, 3f, Offset(98f, 30f))
        drawPath(mouth, ink, style = Stroke(1.8f, cap = StrokeCap.Round))
    }
}

/** deepsea-tang.svg (viewBox 120 x 80). */
private class BlueTang {
    val tail = svgPath("M24 40 L4 18 L11 40 L4 62 Z")
    val topFin = svgPath("M30 24 Q60 4 94 22 L62 32 Z")
    val bottomFin = svgPath("M34 56 Q60 76 90 58 L62 50 Z")
    val body = Path().apply { addOval(Rect(20f, 15f, 104f, 65f)) }
    val stripe = svgPath("M38 31 Q60 20 86 32 Q70 37 77 47 Q60 41 43 51 Q50 40 38 31 Z")
    val smallFin = svgPath("M60 57 Q66 72 75 60 Z")
    val mouth = svgPath("M104 45 Q100 48 96 46")

    fun draw(scope: DrawScope) = with(scope) {
        val yellow = Color(0xFFFFD21F)
        val ink = Color(0xFF10213F)
        drawPath(tail, yellow)
        drawPath(tail, ink, style = Stroke(2.5f, join = StrokeJoin.Round))
        for (fin in listOf(topFin, bottomFin)) {
            drawPath(fin, Color(0xFF1F5FD1))
            drawPath(fin, ink, style = Stroke(2f, join = StrokeJoin.Round))
        }
        drawPath(body, Color(0xFF2F7CF6))
        drawPath(body, ink, style = Stroke(2.5f))
        drawPath(stripe, ink)
        drawPath(smallFin, yellow)
        drawPath(smallFin, ink, style = Stroke(2f, join = StrokeJoin.Round))
        drawCircle(Color.White, 5.5f, Offset(93f, 34f))
        drawCircle(ink, 3.2f, Offset(94f, 34f))
        drawPath(mouth, ink, style = Stroke(1.8f, cap = StrokeCap.Round))
    }
}

@Composable
private fun DeepSeaScene(time: State<Float>) {
    val sand = remember { svgPath("M0 100 C 200 70 420 112 640 92 C 860 72 1100 118 1440 86 L1440 160 L0 160 Z") }
    val weed = remember {
        svgPath(
            "M26 200 C 10 170 42 150 25 120 C 9 92 41 70 25 40 C 19 26 27 12 30 3 C 35 16 31 30 37 42 " +
                "C 51 70 21 92 37 120 C 53 150 21 170 38 200 Z",
        )
    }
    val weedLine = remember { svgPath("M27 196 C 17 174 37 158 29 134 C 21 110 37 92 30 70") }
    val coral = remember { svgPath("M60 118 V70 M60 92 L36 64 V40 M36 64 L20 50 M60 78 L84 54 V30 M84 54 L101 45 M60 70 V44") }
    val tentacles = remember {
        listOf(
            "M20 46 C 14 62 26 74 18 92 C 12 104 20 112 16 118", "M33 48 C 30 66 40 78 34 98",
            "M47 48 C 50 66 40 78 46 100", "M60 46 C 66 62 54 76 62 94 C 66 104 60 112 64 118",
        ).map(::svgPath)
    }
    val bell = remember {
        svgPath("M6 46 C 6 18 22 4 40 4 C 58 4 74 18 74 46 C 66 42 60 50 52 46 C 46 42 34 50 28 46 C 20 42 14 50 6 46 Z")
    }
    val jellySmile = remember { svgPath("M35 34 Q40 38 45 34") }
    val clown = remember { Clownfish() }
    val tang = remember { BlueTang() }
    Spacer(
        Modifier.fillMaxSize().drawWithCache {
            val w = size.width
            val h = size.height
            val dp = density
            val water = Brush.verticalGradient(
                0f to Color(0xFF0B88B3), 0.28f to Color(0xFF06618C), 0.62f to Color(0xFF043E62), 1f to Color(0xFF02223A),
            )
            val rays = rayBands(w, 0.75f * h, dp)
            val rayFill = Brush.verticalGradient(listOf(Color(0x17C8F5FF), Color.Transparent), startY = 0f, endY = 0.75f * h)
            val floorBox = Rect(0f, 0.85f * h, w, h)
            val floorPath = stretched(sand, 1440f, 160f, floorBox)
            val sandFill = vertical(floorPath.getBounds(), 0f to Color(0xFFE9CD8F), 1f to Color(0xFFA9844F))
            val sx = floorBox.width / 1440f
            val sy = floorBox.height / 160f
            fun floorOval(cx: Float, cy: Float, rx: Float, ry: Float) =
                Rect(floorBox.left + (cx - rx) * sx, floorBox.top + (cy - ry) * sy, floorBox.left + (cx + rx) * sx, floorBox.top + (cy + ry) * sy)
            val rocks = listOf(floorOval(300f, 104f, 46f, 18f), floorOval(1160f, 102f, 60f, 20f))
            val specks = listOf(floorOval(520f, 118f, 5f, 2.5f), floorOval(880f, 122f, 6f, 3f), floorOval(1320f, 112f, 5f, 2.5f))
            val bellFill = Brush.radialGradient(listOf(Color(0xFFFFE0F6), Color(0xFFFF6FCF)), center = Offset(40f, 20f), radius = 40f)
            onDrawBehind {
                val t = time.value
                drawRect(water)
                // Light rays from the surface, shimmering.
                val shimmer = ease(alternate(t, 11f))
                withTransform({ translate(lerp(-40f, 40f, shimmer) * dp, 0f) }) {
                    clipRect(-0.1f * w, 0f, 1.1f * w, 0.75f * h) { drawPath(rays, rayFill, alpha = lerp(0.6f, 1f, shimmer)) }
                }
                // A jellyfish bobbing up and down.
                val bob = ease(1 - kotlin.math.abs(frac(t / 7f) * 2 - 1))
                val jellyLeft = w - 0.11f * w - 64 * dp
                val jellyTop = 0.22f * h - 46 * dp * bob
                withTransform({ scale(1 + 0.05f * bob, 1 - 0.05f * bob, Offset(jellyLeft + 32 * dp, jellyTop + 48 * dp)) }) {
                    inViewBox(jellyLeft, jellyTop, 64 * dp / 80f) {
                        for (p in tentacles) {
                            drawPath(p, Color(0xFFFFA6E4), alpha = 0.85f * 0.9f, style = Stroke(3f, cap = StrokeCap.Round))
                        }
                        drawPath(bell, bellFill, alpha = 0.93f * 0.9f)
                        withTransform({ rotate(-25f, Offset(28f, 16f)) }) {
                            drawOval(Color.White, Offset(20f, 12f), Size(16f, 8f), alpha = 0.5f * 0.9f)
                        }
                        drawCircle(Color(0xFF7A1D5C), 2.6f, Offset(31f, 28f), alpha = 0.9f)
                        drawCircle(Color(0xFF7A1D5C), 2.6f, Offset(49f, 28f), alpha = 0.9f)
                        drawPath(jellySmile, Color(0xFF7A1D5C), alpha = 0.9f, style = Stroke(2f, cap = StrokeCap.Round))
                    }
                }
                // A clownfish swimming right, a blue tang swimming left.
                val c = frac(t / 36f)
                val clownX = keyframes(c, listOf(0f to -160 * dp, 0.25f to 0.25f * w, 0.5f to 0.5f * w, 0.75f to 0.75f * w, 1f to w + 40 * dp))
                val clownY = 0.3f * h + keyframes(c, listOf(0f to 0f, 0.25f to -14f, 0.5f to 6f, 0.75f to -10f, 1f to 0f)) * dp
                inViewBox(clownX, clownY, 104 * dp / 120f) { clown.draw(this) }
                val g = frac((t + 22f) / 44f)
                val tangX = keyframes(g, listOf(0f to w + 40 * dp, 0.33f to 0.66f * w, 0.66f to 0.33f * w, 1f to -180 * dp))
                val tangY = 0.56f * h + keyframes(g, listOf(0f to 0f, 0.33f to 12f, 0.66f to -10f, 1f to 0f)) * dp
                val tangWidth = 118 * dp
                withTransform({ scale(-1f, 1f, Offset(tangX + tangWidth / 2, tangY)) }) {
                    inViewBox(tangX, tangY, tangWidth / 120f) { tang.draw(this) }
                }
                // Bubbles rising from the bottom.
                for (b in BUBBLES) drawBubble(b, t, w, h, dp)
                // The sea floor, seaweed and coral.
                drawPath(floorPath, sandFill)
                for (r in rocks) drawOval(Color(0xFF7D8A99), r.topLeft, r.size)
                for (s in specks) drawOval(Color.White, s.topLeft, s.size, alpha = 0.35f)
                for ((left, height, delay) in listOf(Triple(0.03f * w, 0.26f * h, 0f), Triple(w - 0.05f * w - 56 * dp, 0.3f * h, 1f))) {
                    val scale = min(56 * dp / 60f, height / 200f)
                    val drawnLeft = left + (56 * dp - 60f * scale) / 2
                    val bottom = h - 0.07f * h
                    val angle = lerp(-6f, 6f, ease(alternate(t + delay, 4.5f)))
                    withTransform({ rotate(angle, Offset(left + 28 * dp, bottom)) }) {
                        inViewBox(drawnLeft, bottom - 200f * scale, scale) {
                            drawPath(weed, Color(0xFF2FBF71))
                            drawPath(weedLine, Color(0xFF8AF5B6), alpha = 0.55f, style = Stroke(2f, cap = StrokeCap.Round))
                        }
                    }
                }
                inViewBox(0.12f * w, h - 0.05f * h - 104 * dp, 104 * dp / 120f) {
                    drawPath(coral, Color(0xFFFF6F61), style = Stroke(10f, cap = StrokeCap.Round, join = StrokeJoin.Round))
                    for ((x, y) in listOf(36f to 38f, 20f to 49f, 84f to 28f, 101f to 44f, 60f to 42f)) {
                        drawCircle(Color(0xFFFFA194), 6f, Offset(x, y))
                    }
                }
            }
        },
    )
}

private fun DrawScope.drawBubble(b: Bubble, t: Float, w: Float, h: Float, dp: Float) {
    val p = frac((t + b.delay) / b.duration)
    val (dx, dy) = if (p < 0.5f) {
        val q = (p / 0.5f).let { it * it } // ease-in
        lerp(0f, 14 * dp, q) to lerp(0f, -0.55f * h, q)
    } else {
        val q = ((p - 0.5f) / 0.5f).let { it * it }
        lerp(14 * dp, -10 * dp, q) to lerp(-0.55f * h, -1.12f * h, q)
    }
    val alpha = if (p < 0.1f) p / 0.1f else lerp(1f, 0.5f, (p - 0.1f) / 0.9f)
    val size = b.size * dp
    val left = b.left / 100 * w + dx
    val top = h + 24 * dp - size + dy
    val center = Offset(left + size / 2, top + size / 2)
    val shine = Offset(left + 0.32f * size, top + 0.3f * size)
    val reach = 0.976f * size
    drawCircle(
        Brush.radialGradient(
            0f to Color(0xF2FFFFFF), 0.16f to Color(0xF2FFFFFF), 0.42f to Color(0x24FFFFFF), 0.72f to Color(0x0AFFFFFF),
            1f to Color(0x0AFFFFFF), center = shine, radius = reach,
        ),
        radius = size / 2, center = center, alpha = alpha,
    )
    drawCircle(Color(0x8CFFFFFF), radius = size / 2, center = center, alpha = alpha, style = Stroke(1 * dp))
}

/** The light rays: bands at 105 degrees, 45 dp wide every 200 dp (a repeating CSS gradient). */
private fun rayBands(w: Float, h: Float, dp: Float): Path {
    val ux = 0.9659f // the gradient's direction, 105 degrees from "up"
    val uy = 0.2588f
    val vx = -uy // along the bands
    val vy = ux
    val reach = (w + h) * 2
    val corners = listOf(Offset(-0.1f * w, 0f), Offset(1.1f * w, 0f), Offset(-0.1f * w, h), Offset(1.1f * w, h))
    val proj = corners.map { it.x * ux + it.y * uy }
    val period = 200 * dp
    val path = Path()
    var k = floor(proj.min() / period) - 1
    while (k * period < proj.max() + period) {
        val s0 = k * period + 70 * dp
        val s1 = k * period + 115 * dp
        path.moveTo(ux * s0 - vx * reach, uy * s0 - vy * reach)
        path.lineTo(ux * s0 + vx * reach, uy * s0 + vy * reach)
        path.lineTo(ux * s1 + vx * reach, uy * s1 + vy * reach)
        path.lineTo(ux * s1 - vx * reach, uy * s1 - vy * reach)
        path.close()
        k++
    }
    return path
}
