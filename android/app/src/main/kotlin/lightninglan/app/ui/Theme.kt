package lightninglan.app.ui

import androidx.compose.material3.ColorScheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shadow
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.graphics.vector.addPathNodes
import androidx.compose.ui.unit.dp

/** The desktop app's themes (static/style.css), in the order Settings lists them. Synthwave is the default. */
enum class AppTheme(val key: String, val label: String) {
    SYNTHWAVE("synthwave", "Synthwave"),
    BLISS("bliss", "Bliss"),
    DEEPSEA("deepsea", "Deep Sea"),
    LIGHT("light", "Light"),
    DARK("dark", "Dark");

    companion object {
        fun from(key: String?): AppTheme = entries.firstOrNull { it.key == key } ?: SYNTHWAVE
    }
}

/** What a theme changes beyond Material's colours. */
@Immutable
data class AppStyle(
    val theme: AppTheme,
    val dark: Boolean,
    /** A colourful theme: its animated scene is drawn behind the screens, which turn see-through. */
    val scene: Boolean,
    /** For dialogs, which shouldn't be see-through. */
    val solidSurface: Color,
    val heading: Color,
    val headingGlow: Shadow? = null,
    val brand: Brush? = null,
    val topBar: Brush? = null,
    val topBarContent: Color? = null,
    val button: Brush? = null,
    val buttonContent: Color? = null,
    val buttonGlow: Color? = null,
    val bolt: Color = Color(0xFFFBBF24),
    val ok: Color = Color(0xFF16A34A),
)

val LocalAppStyle = staticCompositionLocalOf { lightStyle }

// ----- light and dark (the desktop app's default colours) -----------------------------------------

private val LightColors = lightColorScheme(
    primary = Color(0xFF4F46E5),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFEEF0FF),
    onPrimaryContainer = Color(0xFF1E1B4B),
    secondary = Color(0xFF5D6877),
    tertiary = Color(0xFFF59E0B),
    background = Color(0xFFF5F6F8),
    onBackground = Color(0xFF1B1F27),
    surface = Color(0xFFF5F6F8),
    onSurface = Color(0xFF1B1F27),
    surfaceVariant = Color(0xFFF0F2F5),
    onSurfaceVariant = Color(0xFF5D6877),
    surfaceContainer = Color.White,
    surfaceContainerHigh = Color.White,
    surfaceContainerHighest = Color(0xFFF0F2F5),
    outline = Color(0xFFC9CED8),
    outlineVariant = Color(0xFFE1E4EA),
    error = Color(0xFFDC2626),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFF818CF8),
    onPrimary = Color(0xFF0E1015),
    primaryContainer = Color(0xFF23264A),
    onPrimaryContainer = Color(0xFFE0E7FF),
    secondary = Color(0xFF9AA3B2),
    tertiary = Color(0xFFFBBF24),
    background = Color(0xFF0E1015),
    onBackground = Color(0xFFE6E8EE),
    surface = Color(0xFF0E1015),
    onSurface = Color(0xFFE6E8EE),
    surfaceVariant = Color(0xFF1D212B),
    onSurfaceVariant = Color(0xFF9AA3B2),
    surfaceContainer = Color(0xFF161921),
    surfaceContainerHigh = Color(0xFF1D212B),
    surfaceContainerHighest = Color(0xFF252A35),
    outline = Color(0xFF3A4150),
    outlineVariant = Color(0xFF2A2F3B),
    error = Color(0xFFF87171),
)

private val lightStyle = AppStyle(AppTheme.LIGHT, dark = false, scene = false, solidSurface = Color.White, heading = Color(0xFF1B1F27))
private val darkStyle = AppStyle(
    AppTheme.DARK, dark = true, scene = false, solidSurface = Color(0xFF161921), heading = Color(0xFFE6E8EE), ok = Color(0xFF4ADE80),
)

// ----- Synthwave: an 80s outrun sunset ------------------------------------------------------------

private val SynthwaveColors = darkColorScheme(
    primary = Color(0xFFFF3FBF),
    onPrimary = Color(0xFF170530),
    primaryContainer = Color(0x33FF3FBF),
    onPrimaryContainer = Color(0xFFFDF2FF),
    secondary = Color(0xFFCFAAF0),
    secondaryContainer = Color(0x33FF3FBF),
    onSecondaryContainer = Color(0xFFFDF2FF),
    tertiary = Color(0xFFFFE45C),
    background = Color(0xFF12052A),
    onBackground = Color(0xFFFDF2FF),
    surface = Color(0xFF12052A),
    onSurface = Color(0xFFFDF2FF),
    surfaceVariant = Color(0x9E421A70),
    onSurfaceVariant = Color(0xFFCFAAF0),
    surfaceContainer = Color(0xB8220E42),
    surfaceContainerHigh = Color(0x9E421A70),
    surfaceContainerHighest = Color(0x9E421A70),
    outline = Color(0x61FF4FC4),
    outlineVariant = Color(0x61FF4FC4),
    error = Color(0xFFFF5C7A),
)

private val synthwaveStyle = AppStyle(
    AppTheme.SYNTHWAVE, dark = true, scene = true,
    solidSurface = Color(0xFF2A1150),
    heading = Color(0xFF7FFCFF),
    headingGlow = Shadow(Color(0x8C36F9C6), Offset.Zero, 20f),
    brand = Brush.horizontalGradient(0f to Color(0xFFFF3FBF), 0.55f to Color(0xFFFFE45C), 1f to Color(0xFF36F9C6)),
    topBar = SolidColor(Color(0xB8220E42)),
    buttonGlow = Color(0xFFFF3FBF),
    bolt = Color(0xFFFFE45C),
    ok = Color(0xFF36F9C6),
)

// ----- Bliss: a sunny day over green hills, after the Windows XP wallpaper -------------------------

private val BlissColors = lightColorScheme(
    primary = Color(0xFF2258D6),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFE2ECFF),
    onPrimaryContainer = Color(0xFF12324F),
    secondary = Color(0xFF47657F),
    secondaryContainer = Color(0xFFE2ECFF),
    onSecondaryContainer = Color(0xFF12324F),
    tertiary = Color(0xFFFFB300),
    background = Color(0xFF9FD4FF),
    onBackground = Color(0xFF12324F),
    surface = Color(0xFF9FD4FF),
    onSurface = Color(0xFF12324F),
    surfaceVariant = Color(0xDBE8F3FF),
    onSurfaceVariant = Color(0xFF47657F),
    surfaceContainer = Color(0xD6FFFFFF),
    surfaceContainerHigh = Color(0xDBE8F3FF),
    surfaceContainerHighest = Color(0xDBE8F3FF),
    outline = Color(0x521F5CAA),
    outlineVariant = Color(0x331F5CAA),
    error = Color(0xFFD33A2C),
)

private val blissStyle = AppStyle(
    AppTheme.BLISS, dark = false, scene = true,
    solidSurface = Color(0xFFEAF4FF),
    heading = Color(0xFF1D4FB8),
    topBar = Brush.verticalGradient(0f to Color(0xFF3C8CFF), 0.38f to Color(0xFF2264E3), 0.62f to Color(0xFF1A55CF), 1f to Color(0xFF2A6CF0)),
    topBarContent = Color.White,
    button = Brush.verticalGradient(listOf(Color(0xFF5CC24A), Color(0xFF3A9A2B))), // the green Start button
    buttonContent = Color.White,
    bolt = Color(0xFFFFB300),
    ok = Color(0xFF2F9A3C),
)

// ----- Deep Sea: an aquarium under the waves ----------------------------------------------------------

private val DeepSeaColors = darkColorScheme(
    primary = Color(0xFFFFB347),
    onPrimary = Color(0xFF03263B),
    primaryContainer = Color(0x2EFFB347),
    onPrimaryContainer = Color(0xFFE8FBFF),
    secondary = Color(0xFFA3D6E6),
    secondaryContainer = Color(0x2EFFB347),
    onSecondaryContainer = Color(0xFFE8FBFF),
    tertiary = Color(0xFFFFE066),
    background = Color(0xFF032B43),
    onBackground = Color(0xFFE8FBFF),
    surface = Color(0xFF032B43),
    onSurface = Color(0xFFE8FBFF),
    surfaceVariant = Color(0x9E08486A),
    onSurfaceVariant = Color(0xFFA3D6E6),
    surfaceContainer = Color(0xB3032840),
    surfaceContainerHigh = Color(0x9E08486A),
    surfaceContainerHighest = Color(0x9E08486A),
    outline = Color(0x426EE6FF),
    outlineVariant = Color(0x426EE6FF),
    error = Color(0xFFFF7B7B),
)

private val deepSeaStyle = AppStyle(
    AppTheme.DEEPSEA, dark = true, scene = true,
    solidSurface = Color(0xFF064463),
    heading = Color(0xFF7FE8FF),
    topBar = SolidColor(Color(0xB3032840)),
    bolt = Color(0xFFFFE066),
    ok = Color(0xFF5EF0B4),
)

fun styleFor(theme: AppTheme): AppStyle = when (theme) {
    AppTheme.LIGHT -> lightStyle
    AppTheme.DARK -> darkStyle
    AppTheme.SYNTHWAVE -> synthwaveStyle
    AppTheme.BLISS -> blissStyle
    AppTheme.DEEPSEA -> deepSeaStyle
}

private fun colorsFor(style: AppStyle): ColorScheme = when (style.theme) {
    AppTheme.SYNTHWAVE -> SynthwaveColors
    AppTheme.BLISS -> BlissColors
    AppTheme.DEEPSEA -> DeepSeaColors
    AppTheme.DARK -> DarkColors
    else -> LightColors
}

@Composable
fun LightningTheme(style: AppStyle, content: @Composable () -> Unit) {
    CompositionLocalProvider(LocalAppStyle provides style) {
        MaterialTheme(colorScheme = colorsFor(style), content = content)
    }
}

/** The few icons the core icon set lacks. */
object AppIcons {
    val Bolt = icon("Bolt", "M13 3 5 14h6l-1 7 8-11h-6l1-7z")
    val Pause = icon("Pause", "M6 19h4V5H6v14zm8-14v14h4V5h-4z")
    val Download = icon("Download", "M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z")
    val Gamepad = icon(
        "Gamepad",
        "M21.58 16.09l-1.09-7.66C20.21 6.46 18.52 5 16.53 5H7.47C5.48 5 3.79 6.46 3.51 8.43l-1.09 7.66C2.2 17.63 3.39 19 " +
            "4.94 19c.68 0 1.32-.27 1.8-.75L9 16h6l2.25 2.25c.48.48 1.13.75 1.8.75 1.56 0 2.75-1.37 2.53-2.91zM11 11H9v2H8v-2H6" +
            "v-1h2V8h1v2h2v1zm4-1c-.55 0-1-.45-1-1s.45-1 1-1 1 .45 1 1-.45 1-1 1zm2 3c-.55 0-1-.45-1-1s.45-1 1-1 1 .45 1 1-.45 1-1 1z",
    )
    val Phone = icon("Phone", "M17 1.01 7 1c-1.1 0-2 .9-2 2v18c0 1.1.9 2 2 2h10c1.1 0 2-.9 2-2V3c0-1.1-.9-1.99-2-1.99zM17 19H7V5h10v14z")
    val Folder = icon("Folder", "M10 4H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z")
    val Dice = icon(
        "Dice",
        "M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zM7.5 18c-.83 0-1.5-.67-1.5-1.5S6.67 15 7.5 15" +
            "s1.5.67 1.5 1.5S8.33 18 7.5 18zm0-9C6.67 9 6 8.33 6 7.5S6.67 6 7.5 6 9 6.67 9 7.5 8.33 9 7.5 9zm4.5 4.5c-.83 0-1.5-.67-1.5-1.5" +
            "s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5zm4.5 4.5c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5z" +
            "m0-9c-.83 0-1.5-.67-1.5-1.5S15.67 6 16.5 6s1.5.67 1.5 1.5S17.33 9 16.5 9z",
    )

    /** The desktop app's theme button: a half-filled circle. */
    val Theme: ImageVector = ImageVector.Builder("Theme", 24.dp, 24.dp, 24f, 24f)
        .addPath(addPathNodes("M12 3a9 9 0 1 0 0 18V3z"), fill = SolidColor(Color.Black))
        .addPath(
            addPathNodes("M12 3.75a8.25 8.25 0 1 0 0 16.5a8.25 8.25 0 1 0 0-16.5z"),
            stroke = SolidColor(Color.Black), strokeLineWidth = 1.5f,
        )
        .build()

    private fun icon(name: String, path: String): ImageVector =
        ImageVector.Builder(name, 24.dp, 24.dp, 24f, 24f).addPath(addPathNodes(path), fill = SolidColor(Color.Black)).build()
}
