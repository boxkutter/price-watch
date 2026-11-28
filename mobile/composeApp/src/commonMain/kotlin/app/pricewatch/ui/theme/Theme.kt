package app.pricewatch.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/** Colours that mean something on a price: cheaper, dearer, and a warm accent for offers. */
@Immutable
class PriceColors(val cheaper: Color, val cheaperContainer: Color, val dearer: Color, val dearerContainer: Color, val offer: Color)

private val LightPrice = PriceColors(Color(0xFF15803D), Color(0xFFDDF4E6), Color(0xFFC2410C), Color(0xFFFCE5DA), Color(0xFFB45309))
private val DarkPrice = PriceColors(Color(0xFF5FD38D), Color(0xFF123824), Color(0xFFFB923C), Color(0xFF412211), Color(0xFFFBBF24))

val LocalPriceColors = staticCompositionLocalOf { LightPrice }

private val Light = lightColorScheme(
    primary = Color(0xFF0B6E4F), onPrimary = Color.White,
    primaryContainer = Color(0xFFD3F0E2), onPrimaryContainer = Color(0xFF05301F),
    secondary = Color(0xFF4A6358), onSecondary = Color.White,
    secondaryContainer = Color(0xFFDCE8E1), onSecondaryContainer = Color(0xFF14231C),
    tertiary = Color(0xFFB45309), onTertiary = Color.White,
    background = Color(0xFFF5F8F6), onBackground = Color(0xFF16211D),
    surface = Color(0xFFF5F8F6), onSurface = Color(0xFF16211D),
    surfaceVariant = Color(0xFFE6EEEA), onSurfaceVariant = Color(0xFF55645E),
    surfaceContainerLowest = Color.White, surfaceContainerLow = Color(0xFFFBFDFC),
    surfaceContainer = Color(0xFFEFF4F1), surfaceContainerHigh = Color(0xFFE8EFEC), surfaceContainerHighest = Color(0xFFE1E9E5),
    outline = Color(0xFF8A9993), outlineVariant = Color(0xFFD9E2DE),
    error = Color(0xFFBA1A1A), onError = Color.White, errorContainer = Color(0xFFFFDAD6), onErrorContainer = Color(0xFF410002),
)

private val Dark = darkColorScheme(
    primary = Color(0xFF6EDDAE), onPrimary = Color(0xFF00382A),
    primaryContainer = Color(0xFF0F4D38), onPrimaryContainer = Color(0xFFC4F2DD),
    secondary = Color(0xFFB0CCBF), onSecondary = Color(0xFF1B352B),
    secondaryContainer = Color(0xFF2F4B40), onSecondaryContainer = Color(0xFFCCE8DB),
    tertiary = Color(0xFFFBBF24), onTertiary = Color(0xFF3F2C00),
    background = Color(0xFF0D1311), onBackground = Color(0xFFE4EDE9),
    surface = Color(0xFF0D1311), onSurface = Color(0xFFE4EDE9),
    surfaceVariant = Color(0xFF26312D), onSurfaceVariant = Color(0xFF9DADA6),
    surfaceContainerLowest = Color(0xFF080D0B), surfaceContainerLow = Color(0xFF121A17),
    surfaceContainer = Color(0xFF161F1C), surfaceContainerHigh = Color(0xFF1C2724), surfaceContainerHighest = Color(0xFF232F2B),
    outline = Color(0xFF6C7B75), outlineVariant = Color(0xFF2B3733),
    error = Color(0xFFFFB4AB), onError = Color(0xFF690005), errorContainer = Color(0xFF93000A), onErrorContainer = Color(0xFFFFDAD6),
)

private val AppTypography = Typography().let { base ->
    base.copy(
        headlineMedium = base.headlineMedium.copy(fontWeight = FontWeight.Bold, fontSize = 30.sp, lineHeight = 36.sp),
        titleLarge = base.titleLarge.copy(fontWeight = FontWeight.SemiBold),
        titleMedium = base.titleMedium.copy(fontWeight = FontWeight.SemiBold),
        labelLarge = base.labelLarge.copy(fontWeight = FontWeight.SemiBold),
    )
}

private val AppShapes = Shapes(
    extraSmall = RoundedCornerShape(8.dp),
    small = RoundedCornerShape(12.dp),
    medium = RoundedCornerShape(16.dp),
    large = RoundedCornerShape(22.dp),
    extraLarge = RoundedCornerShape(30.dp),
)

@Composable
fun PriceWatchTheme(dark: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    CompositionLocalProvider(LocalPriceColors provides if (dark) DarkPrice else LightPrice) {
        MaterialTheme(
            colorScheme = if (dark) Dark else Light,
            typography = AppTypography,
            shapes = AppShapes,
            content = content,
        )
    }
}

val priceColors: PriceColors @Composable @ReadOnlyComposable get() = LocalPriceColors.current

/** A price: bold with fixed-width digits, so a column of them lines up. */
val PriceStyle = TextStyle(fontWeight = FontWeight.Bold, fontFeatureSettings = "tnum")
