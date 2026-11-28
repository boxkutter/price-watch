package app.pricewatch.ui.monitor

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import app.pricewatch.shared.api.PricePoint
import app.pricewatch.shared.format.toAmount
import kotlin.time.Instant

/** One price at one moment; the chart keeps each price until the next one is seen. */
data class ChartPoint(val at: Long, val price: Double)

/** Price history (newest first, as the API gives it) as chart points, oldest first. Prices of unknown value are skipped. */
fun chartPoints(history: List<PricePoint>): List<ChartPoint> =
    history.mapNotNull { p ->
        val price = p.currentPrice.toAmount() ?: return@mapNotNull null
        val at = runCatching { Instant.parse(p.observedAt).toEpochMilliseconds() }.getOrNull() ?: return@mapNotNull null
        ChartPoint(at, price)
    }.sortedBy { it.at }

/** A stepped line chart of a price over time, with the person's target as a dashed line. */
@Composable
fun PriceChart(points: List<ChartPoint>, target: Double?, modifier: Modifier = Modifier, lineColor: Color = MaterialTheme.colorScheme.primary) {
    val grid = MaterialTheme.colorScheme.outlineVariant
    val targetColor = MaterialTheme.colorScheme.tertiary
    Canvas(modifier.fillMaxWidth().height(150.dp)) {
        if (points.isEmpty()) return@Canvas
        val values = points.map { it.price } + listOfNotNull(target)
        val lo = values.min(); val hi = values.max()
        val pad = ((hi - lo).takeIf { it > 0 } ?: (hi * 0.1).coerceAtLeast(1.0)) * 0.18
        val min = lo - pad; val max = hi + pad
        val t0 = points.first().at; val t1 = points.last().at
        val inset = 6.dp.toPx()
        fun x(t: Long) = if (t1 == t0) size.width / 2 else inset + (t - t0).toFloat() / (t1 - t0) * (size.width - 2 * inset)
        fun y(v: Double) = size.height - ((v - min) / (max - min)).toFloat() * size.height

        for (i in 0..3) {
            val gy = size.height * i / 3f
            drawLine(grid, Offset(0f, gy), Offset(size.width, gy), strokeWidth = 1.dp.toPx())
        }
        target?.let {
            drawLine(targetColor, Offset(0f, y(it)), Offset(size.width, y(it)), strokeWidth = 1.5.dp.toPx(),
                pathEffect = PathEffect.dashPathEffect(floatArrayOf(10f, 10f)))
        }
        // the line: horizontal until the next price is seen, then straight to it
        val line = Path().apply {
            moveTo(x(points.first().at), y(points.first().price))
            for (i in 1 until points.size) {
                lineTo(x(points[i].at), y(points[i - 1].price))
                lineTo(x(points[i].at), y(points[i].price))
            }
            lineTo(size.width - inset, y(points.last().price))
        }
        val fill = Path().apply {
            addPath(line); lineTo(size.width - inset, size.height); lineTo(x(points.first().at), size.height); close()
        }
        drawPath(fill, Brush.verticalGradient(listOf(lineColor.copy(alpha = 0.22f), lineColor.copy(alpha = 0f))))
        drawPath(line, lineColor, style = Stroke(width = 2.5.dp.toPx(), cap = StrokeCap.Round, join = StrokeJoin.Round))
        drawCircle(lineColor, 5.dp.toPx(), Offset(size.width - inset, y(points.last().price)))
    }
}
