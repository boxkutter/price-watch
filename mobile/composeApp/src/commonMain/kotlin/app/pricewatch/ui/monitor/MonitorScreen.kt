package app.pricewatch.ui.monitor

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.ui.draw.clip
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.backhandler.BackHandler
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.pricewatch.shared.api.Monitor
import app.pricewatch.shared.format.formatMoney
import app.pricewatch.shared.format.formatUnitPrice
import app.pricewatch.shared.format.priceChange
import app.pricewatch.shared.format.relativeTime
import app.pricewatch.shared.format.shortDate
import app.pricewatch.shared.format.toAmount
import app.pricewatch.shared.notify.Notice
import app.pricewatch.shared.notify.NoticeKind
import app.pricewatch.ui.AppModel
import app.pricewatch.ui.MonitorView
import app.pricewatch.ui.ScanPurpose
import app.pricewatch.ui.Tab
import app.pricewatch.ui.components.AlertPriceDialog
import app.pricewatch.ui.components.ChangePill
import app.pricewatch.ui.components.EmptyState
import app.pricewatch.ui.components.ErrorCard
import app.pricewatch.ui.components.Pill
import app.pricewatch.ui.components.ProductImage
import app.pricewatch.ui.components.RetailerBadge
import app.pricewatch.ui.components.ScreenTitle
import app.pricewatch.ui.components.SoftCard
import app.pricewatch.ui.theme.AppIcons
import app.pricewatch.ui.theme.PriceStyle
import app.pricewatch.ui.theme.priceColors

@Composable
fun MonitorScreen(model: AppModel) {
    val selected by model.monitors.selectedId.collectAsState()
    val items by model.monitors.items.collectAsState()
    val current = items.firstOrNull { it.id == selected }
    if (current != null) {
        BackHandler { model.monitors.close() }
        MonitorDetail(current, model)
    } else {
        MonitorList(model)
    }
}

/** The alert price is reached by one item's price, or by the price per item of a multi-buy. */
private fun Monitor.reachedTarget(): Boolean {
    val target = conditions.maximumPrice.toAmount() ?: return false
    val price = latest?.currentPrice.toAmount()
    val each = offer?.unitPrice.toAmount()
    return (price != null && price <= target) || (each != null && each <= target)
}

@Composable
private fun MonitorList(model: AppModel) {
    val monitors = model.monitors
    val items by monitors.items.collectAsState()
    val loading by monitors.loading.collectAsState()
    val loadedOnce by monitors.loadedOnce.collectAsState()
    val error by monitors.error.collectAsState()
    val view by model.monitorView.collectAsState()
    val activity by monitors.activity.collectAsState()
    val activityLoading by monitors.activityLoading.collectAsState()

    LaunchedEffect(view) { if (view == MonitorView.Activity) monitors.refreshActivity() }

    Column(Modifier.fillMaxSize()) {
        ScreenTitle("Monitor")
        SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth().padding(horizontal = 16.dp)) {
            MonitorView.entries.forEachIndexed { i, v ->
                SegmentedButton(
                    selected = view == v, onClick = { model.setMonitorView(v) },
                    shape = SegmentedButtonDefaults.itemShape(i, MonitorView.entries.size),
                    label = { Text(if (v == MonitorView.Watching) "Watching${if (items.isNotEmpty()) " (${items.size})" else ""}" else "Activity") },
                )
            }
        }
        PullToRefreshBox(
            isRefreshing = if (view == MonitorView.Watching) loading else activityLoading,
            onRefresh = { if (view == MonitorView.Watching) monitors.refresh() else monitors.refreshActivity() },
            modifier = Modifier.fillMaxSize().padding(top = 8.dp),
        ) {
            if (view == MonitorView.Watching) {
                LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(start = 16.dp, end = 16.dp, bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    error?.let { item { ErrorCard(it, onRetry = monitors::refresh) } }
                    if (items.isEmpty() && loadedOnce && error == null) item {
                        EmptyState(AppIcons.Eye, "Nothing watched yet", "Find a product in Lookup and tap Watch. You will get a notification when its price drops or reaches your target.",
                            actionLabel = "Find a product", onAction = { model.select(Tab.Lookup) })
                    }
                    items(items, key = { it.id }) { m -> MonitorCard(m, model) { monitors.open(m.id) } }
                }
            } else {
                LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(start = 16.dp, end = 16.dp, bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (activity.isEmpty() && !activityLoading) item {
                        EmptyState(AppIcons.TrendDown, "No activity yet", "Price drops, offers and restocks on the products you watch show up here.")
                    }
                    items(activity, key = { it.eventId }) { NoticeRow(it) }
                }
            }
        }
    }
}

@Composable
internal fun NoticeRow(notice: Notice, modifier: Modifier = Modifier) {
    val c = priceColors
    val (container, content) = when (notice.kind) {
        NoticeKind.PriceDrop, NoticeKind.TargetReached, NoticeKind.Restocked -> c.cheaperContainer to c.cheaper
        NoticeKind.PriceIncrease, NoticeKind.Unavailable -> c.dearerContainer to c.dearer
        else -> MaterialTheme.colorScheme.secondaryContainer to MaterialTheme.colorScheme.onSecondaryContainer
    }
    SoftCard(modifier.fillMaxWidth()) {
        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            androidx.compose.foundation.layout.Box(
                Modifier.size(36.dp).clip(CircleShape).background(container),
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    when (notice.kind) {
                        NoticeKind.PriceIncrease -> AppIcons.ArrowUp
                        NoticeKind.TargetReached -> Icons.Default.Check
                        NoticeKind.PriceDrop -> AppIcons.ArrowDown
                        else -> AppIcons.TrendDown
                    },
                    null, Modifier.size(20.dp), tint = content,
                )
            }
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(notice.title, style = MaterialTheme.typography.titleSmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(notice.text, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text(relativeTime(notice.createdAt), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline)
            }
        }
    }
}

@Composable
private fun MonitorCard(m: Monitor, model: AppModel, onClick: () -> Unit) {
    val latest = m.latest
    val change = priceChange(latest?.currentPrice, latest?.previousPrice)
    val reached = m.reachedTarget()
    SoftCard(Modifier.fillMaxWidth(), onClick = onClick) {
        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            ProductImage(model.graph.api.imageUrl(m.image), 68.dp)
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Text(m.productName ?: m.name, style = MaterialTheme.typography.titleMedium, maxLines = 2, overflow = TextOverflow.Ellipsis)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    RetailerBadge(model.directory.retailerName(m.retailerId), model.directory.logoUrl(m.retailerId), 18.dp)
                    Spacer(Modifier.width(6.dp))
                    Text(m.storeName ?: "", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                m.offer?.let { deal ->
                    Text(
                        listOfNotNull(formatMoney(deal.unitPrice, latest?.currency ?: "SEK") + if (deal.quantity > 1) " each" else "", if (deal.quantity == 1 && deal.member) "member price" else deal.terms).joinToString(" · "),
                        style = MaterialTheme.typography.bodySmall, color = priceColors.offer, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    )
                }
                val target = m.conditions.maximumPrice
                Text(
                    when {
                        latest == null -> "Waiting for the first price"
                        target != null -> "Target ${formatMoney(target, latest.currency)}"
                        else -> "Checked ${relativeTime(latest.observedAt)}"
                    },
                    style = MaterialTheme.typography.bodySmall, color = if (reached) priceColors.cheaper else MaterialTheme.colorScheme.outline,
                )
            }
            Spacer(Modifier.width(8.dp))
            Column(horizontalAlignment = Alignment.End, verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(formatMoney(latest?.currentPrice, latest?.currency ?: "SEK"), style = MaterialTheme.typography.titleLarge.merge(PriceStyle))
                ChangePill(change, formatMoney(kotlin.math.abs(change ?: 0.0), latest?.currency ?: "SEK"))
                if (reached) Pill("Target reached", priceColors.cheaperContainer, priceColors.cheaper, icon = Icons.Default.Check)
                else if (latest?.availability == "out_of_stock") Pill("Out of stock", priceColors.dearerContainer, priceColors.dearer)
            }
        }
    }
}

@Composable
private fun MonitorDetail(m: Monitor, model: AppModel) {
    val monitors = model.monitors
    val history by monitors.history.collectAsState()
    val notices by monitors.monitorNotices.collectAsState()
    val checking by monitors.checking.collectAsState()
    var confirmRemove by remember { mutableStateOf(false) }
    var editAlert by remember { mutableStateOf(false) }
    val linkedNow by model.barcodes.linked.collectAsState()
    val latest = m.latest
    val currency = latest?.currency ?: "SEK"
    val points = remember(history) { chartPoints(history.orEmpty()) }
    val target = m.conditions.maximumPrice.toAmount()
    val sourceUrl = history?.firstOrNull()?.sourceUrl

    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        item {
            Row(Modifier.padding(horizontal = 8.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = monitors::close) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
                Text("Product", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f))
                IconButton(onClick = { confirmRemove = true }) { Icon(Icons.Default.Delete, "Remove", tint = MaterialTheme.colorScheme.error) }
            }
        }
        item {
            Row(Modifier.padding(horizontal = 20.dp), verticalAlignment = Alignment.CenterVertically) {
                ProductImage(model.graph.api.imageUrl(m.image), 92.dp)
                Spacer(Modifier.width(16.dp))
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(m.productName ?: m.name, style = MaterialTheme.typography.titleLarge, maxLines = 3)
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        RetailerBadge(model.directory.retailerName(m.retailerId), model.directory.logoUrl(m.retailerId), 20.dp)
                        Spacer(Modifier.width(6.dp))
                        Text(m.storeName ?: "", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
        item {
            SoftCard(Modifier.padding(horizontal = 16.dp).fillMaxWidth()) {
                Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Row(verticalAlignment = Alignment.Bottom) {
                        Text(formatMoney(latest?.currentPrice, currency), style = MaterialTheme.typography.headlineMedium.merge(PriceStyle))
                        Spacer(Modifier.width(10.dp))
                        val change = priceChange(latest?.currentPrice, latest?.previousPrice)
                        ChangePill(change, formatMoney(kotlin.math.abs(change ?: 0.0), currency), Modifier.padding(bottom = 6.dp))
                    }
                    val unit = formatUnitPrice(latest?.unitPrice?.amount, latest?.unitPrice?.unit, currency)
                    Text(
                        listOfNotNull(unit, target?.let { "Target ${formatMoney(it, currency)}" }, latest?.observedAt?.let { "Checked ${relativeTime(it)}" }).joinToString(" · "),
                        style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    when {
                        history == null -> LinearProgressIndicator(Modifier.fillMaxWidth())
                        points.size < 2 -> Text("The price history fills in as the price is checked.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline)
                        else -> {
                            PriceChart(points, target, Modifier.padding(top = 4.dp))
                            val lo = points.minOf { it.price }; val hi = points.maxOf { it.price }
                            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                                Text("Lowest ${formatMoney(lo, currency)}", style = MaterialTheme.typography.bodySmall, color = priceColors.cheaper)
                                Text("Highest ${formatMoney(hi, currency)}", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }
                    }
                }
            }
        }
        item {
            SoftCard(Modifier.padding(horizontal = 16.dp).fillMaxWidth()) {
                Column(Modifier.padding(vertical = 4.dp)) {
                    DetailRow(
                        "Alert price",
                        if (target != null) formatMoney(target, currency) else "Not set",
                        if (target != null) "Change" else "Set",
                    ) { editAlert = true }
                    m.offer?.let { deal ->
                        DetailRow(
                            if (deal.member) "Your member price" else "Best deal",
                            listOfNotNull(formatMoney(deal.unitPrice, currency) + if (deal.quantity > 1) " each" else "", deal.terms, if (deal.quantity > 1) "buy ${deal.quantity}" else null, deal.validTo?.let { "until ${shortDate(it)}" }).joinToString(" · "),
                            null,
                        ) {}
                    }
                    val barcode = m.barcode
                    val productId = m.productId
                    when {
                        barcode == null && productId != null && productId !in linkedNow ->
                            DetailRow("Barcode", "None yet", "Scan to add") {
                                model.startScan(ScanPurpose.LinkBarcode(productId, m.productName ?: m.name))
                            }
                        barcode == null -> DetailRow("Barcode", "Added", null) {}
                        barcode.source == "user" && productId != null ->
                            DetailRow("Barcode", "${barcode.gtin14.trimStart('0')} (added by you)", "Remove") { monitors.let { model.barcodes.unlink(productId) } }
                        else -> DetailRow("Barcode", barcode.gtin14.trimStart('0'), null) {}
                    }
                }
            }
        }
        item {
            Row(Modifier.padding(horizontal = 16.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(onClick = { monitors.checkNow(m.id) }, enabled = !checking, modifier = Modifier.weight(1f)) {
                    if (checking) CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = MaterialTheme.colorScheme.onPrimary)
                    else Icon(Icons.Default.Refresh, null, Modifier.size(18.dp))
                    Spacer(Modifier.width(8.dp)); Text("Check now")
                }
                OutlinedButton(onClick = { sourceUrl?.let(model.platform::openUrl) }, enabled = sourceUrl != null, modifier = Modifier.weight(1f)) {
                    Icon(AppIcons.OpenInNew, null, Modifier.size(18.dp)); Spacer(Modifier.width(8.dp)); Text("Open in store")
                }
            }
        }
        if (notices.isNotEmpty()) {
            item { Text("History", style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(horizontal = 20.dp, vertical = 4.dp)) }
            items(notices.take(30), key = { it.eventId }) { NoticeRow(it, Modifier.padding(horizontal = 16.dp)) }
        }
    }
    if (editAlert) {
        AlertPriceDialog(
            productName = m.productName ?: m.name,
            nowPrice = latest?.currentPrice,
            current = m.conditions.maximumPrice,
            onDismiss = { editAlert = false },
            onSave = { price -> editAlert = false; monitors.setAlertPrice(m.id, price) },
        )
    }
    if (confirmRemove) {
        AlertDialog(
            onDismissRequest = { confirmRemove = false },
            title = { Text("Stop watching?") },
            text = { Text("${m.productName ?: m.name} leaves your watchlist and you will not get alerts for it.") },
            confirmButton = {
                Button(onClick = { confirmRemove = false; monitors.remove(m.id) }, colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error)) { Text("Remove") }
            },
            dismissButton = { TextButton(onClick = { confirmRemove = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun DetailRow(label: String, value: String, action: String?, onAction: () -> Unit) {
    Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 8.dp, top = 2.dp, bottom = 2.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f).padding(vertical = 8.dp)) {
            Text(label, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text(value, style = MaterialTheme.typography.bodyLarge)
        }
        if (action != null) TextButton(onClick = onAction) { Text(action) }
    }
}
