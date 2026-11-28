package app.pricewatch.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ShoppingCart
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import app.pricewatch.shared.api.ImageRef
import app.pricewatch.ui.theme.AppIcons
import app.pricewatch.ui.theme.priceColors
import coil3.compose.AsyncImage

/** A product picture, or a quiet placeholder when there is none. */
@Composable
fun ProductImage(url: String?, size: Dp = 64.dp, modifier: Modifier = Modifier) {
    Box(
        modifier.size(size).clip(RoundedCornerShape(size / 4.5f)).background(MaterialTheme.colorScheme.surfaceContainerLowest),
        contentAlignment = Alignment.Center,
    ) {
        if (url != null) {
            AsyncImage(model = url, contentDescription = null, contentScale = ContentScale.Fit, modifier = Modifier.fillMaxSize().padding(size / 12))
        } else {
            Icon(Icons.Default.ShoppingCart, null, Modifier.size(size / 2.5f), tint = MaterialTheme.colorScheme.outline)
        }
    }
}

/** The retailer's logo, or its initials on a colour taken from its name. */
@Composable
fun RetailerBadge(name: String, logoUrl: String?, size: Dp = 28.dp, modifier: Modifier = Modifier) {
    if (logoUrl != null) {
        Box(modifier.size(size).clip(CircleShape).background(Color.White), contentAlignment = Alignment.Center) {
            AsyncImage(model = logoUrl, contentDescription = name, modifier = Modifier.fillMaxSize().padding(size / 7))
        }
    } else {
        val hue = (name.fold(7) { h, c -> h * 31 + c.code }.mod(360)).toFloat()
        val bg = Color.hsl(hue, 0.45f, 0.42f)
        Box(modifier.size(size).clip(CircleShape).background(bg), contentAlignment = Alignment.Center) {
            Text(
                name.split(' ', '.').filter { it.isNotEmpty() }.take(2).joinToString("") { it.first().uppercase() },
                color = Color.White, fontSize = (size.value * 0.4f).sp, fontWeight = FontWeight.Bold,
            )
        }
    }
}

fun imageUrlOf(ref: ImageRef?, absolute: (String?) -> String?): String? = ref?.let { absolute(it.url) }

/** A small pill of text: "Offer", "Cheapest", "In stock". */
@Composable
fun Pill(text: String, container: Color, content: Color, modifier: Modifier = Modifier, icon: ImageVector? = null) {
    Surface(modifier, shape = CircleShape, color = container, contentColor = content) {
        Row(Modifier.padding(horizontal = 10.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            if (icon != null) {
                Icon(icon, null, Modifier.size(14.dp))
                Spacer(Modifier.size(4.dp))
            }
            Text(text, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.SemiBold)
        }
    }
}

/** How a price moved: green and down when cheaper, orange and up when dearer. Nothing when unchanged or unknown. */
@Composable
fun ChangePill(change: Double?, formatted: String, modifier: Modifier = Modifier) {
    if (change == null || change == 0.0) return
    val c = priceColors
    if (change < 0) Pill(formatted, c.cheaperContainer, c.cheaper, modifier, AppIcons.ArrowDown)
    else Pill(formatted, c.dearerContainer, c.dearer, modifier, AppIcons.ArrowUp)
}

@Composable
fun ScreenTitle(title: String, modifier: Modifier = Modifier, trailing: @Composable () -> Unit = {}) {
    Row(modifier.fillMaxWidth().padding(horizontal = 20.dp, vertical = 12.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(title, style = MaterialTheme.typography.headlineMedium, modifier = Modifier.weight(1f))
        trailing()
    }
}

@Composable
fun EmptyState(
    icon: ImageVector,
    title: String,
    text: String,
    modifier: Modifier = Modifier,
    actionLabel: String? = null,
    onAction: (() -> Unit)? = null,
) {
    Column(
        modifier.fillMaxWidth().padding(horizontal = 36.dp, vertical = 40.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Box(Modifier.size(72.dp).clip(CircleShape).background(MaterialTheme.colorScheme.primaryContainer), contentAlignment = Alignment.Center) {
            Icon(icon, null, Modifier.size(34.dp), tint = MaterialTheme.colorScheme.onPrimaryContainer)
        }
        Spacer(Modifier.height(4.dp))
        Text(title, style = MaterialTheme.typography.titleLarge, textAlign = TextAlign.Center)
        Text(text, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, textAlign = TextAlign.Center)
        if (actionLabel != null && onAction != null) {
            Spacer(Modifier.height(6.dp))
            Button(onClick = onAction) { Text(actionLabel) }
        }
    }
}

/** An error in plain words, with a way to dismiss it or try again. */
@Composable
fun ErrorCard(message: String, modifier: Modifier = Modifier, onRetry: (() -> Unit)? = null, onDismiss: (() -> Unit)? = null) {
    Surface(modifier.fillMaxWidth(), shape = MaterialTheme.shapes.medium, color = MaterialTheme.colorScheme.errorContainer) {
        Row(Modifier.padding(start = 14.dp, top = 8.dp, bottom = 8.dp, end = 6.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Default.Warning, null, Modifier.size(20.dp), tint = MaterialTheme.colorScheme.onErrorContainer)
            Text(message, Modifier.weight(1f).padding(horizontal = 10.dp), color = MaterialTheme.colorScheme.onErrorContainer, style = MaterialTheme.typography.bodyMedium)
            if (onRetry != null) TextButton(onClick = onRetry) { Text("Retry") }
            else if (onDismiss != null) TextButton(onClick = onDismiss) { Text("OK") }
        }
    }
}

/** A white card on the soft background: the app's one surface style. */
@Composable
fun SoftCard(modifier: Modifier = Modifier, onClick: (() -> Unit)? = null, content: @Composable () -> Unit) {
    val colors = MaterialTheme.colorScheme.surfaceContainerLowest
    if (onClick != null) {
        Surface(onClick = onClick, modifier = modifier, shape = MaterialTheme.shapes.large, color = colors, tonalElevation = 0.dp, shadowElevation = 0.dp, content = content)
    } else {
        Surface(modifier = modifier, shape = MaterialTheme.shapes.large, color = colors, tonalElevation = 0.dp, shadowElevation = 0.dp, content = content)
    }
}

@Composable
fun SectionLabel(text: String, modifier: Modifier = Modifier) {
    Text(
        text.uppercase(), modifier.padding(horizontal = 24.dp, vertical = 8.dp),
        style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, letterSpacing = 0.8.sp,
    )
}

