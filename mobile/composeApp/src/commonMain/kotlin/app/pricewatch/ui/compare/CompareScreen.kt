package app.pricewatch.ui.compare

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.LocationOn
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.backhandler.BackHandler
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.pricewatch.shared.api.CompareRow
import app.pricewatch.shared.format.formatMoney
import app.pricewatch.shared.format.formatUnitPrice
import app.pricewatch.shared.format.toAmount
import app.pricewatch.ui.AppModel
import app.pricewatch.ui.ScanPurpose
import app.pricewatch.ui.components.EmptyState
import app.pricewatch.ui.components.ErrorCard
import app.pricewatch.ui.components.dealNote
import app.pricewatch.ui.components.Pill
import app.pricewatch.ui.components.RetailerBadge
import app.pricewatch.ui.components.ScreenTitle
import app.pricewatch.ui.components.SoftCard
import app.pricewatch.ui.theme.AppIcons
import app.pricewatch.ui.theme.PriceStyle
import app.pricewatch.ui.theme.priceColors

@Composable
fun CompareScreen(model: AppModel) {
    val compare = model.compare
    val state by compare.state.collectAsState()
    val typed by compare.typed.collectAsState()
    val here by compare.hereStoreId.collectAsState()
    val mine by model.directory.mine.collectAsState()

    LazyColumn(contentPadding = PaddingValues(bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item { ScreenTitle("Compare") }
        item {
            SoftCard(Modifier.padding(horizontal = 16.dp).fillMaxWidth(), onClick = { model.startScan(ScanPurpose.Compare) }) {
                Row(Modifier.padding(18.dp), verticalAlignment = Alignment.CenterVertically) {
                    Box(Modifier.size(52.dp).clip(RoundedCornerShape(16.dp)).background(MaterialTheme.colorScheme.primary), contentAlignment = Alignment.Center) {
                        Icon(AppIcons.QrScan, null, tint = MaterialTheme.colorScheme.onPrimary, modifier = Modifier.size(28.dp))
                    }
                    Spacer(Modifier.width(16.dp))
                    Column {
                        Text("Scan a barcode", style = MaterialTheme.typography.titleMedium)
                        Text("In a shop? See where else it is cheaper.", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
        item {
            TextField(
                value = typed, onValueChange = compare::setTyped, singleLine = true,
                placeholder = { Text("Or type the barcode number") },
                trailingIcon = {
                    if (typed.isNotEmpty()) FilledTonalIconButton(onClick = compare::lookupTyped, colors = IconButtonDefaults.filledTonalIconButtonColors()) {
                        Icon(AppIcons.Swap, "Compare")
                    }
                },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number, imeAction = ImeAction.Search),
                keyboardActions = KeyboardActions(onSearch = { compare.lookupTyped() }),
                shape = MaterialTheme.shapes.extraLarge,
                colors = TextFieldDefaults.colors(
                    focusedContainerColor = MaterialTheme.colorScheme.surfaceContainerLowest, unfocusedContainerColor = MaterialTheme.colorScheme.surfaceContainerLowest,
                    focusedIndicatorColor = Color.Transparent, unfocusedIndicatorColor = Color.Transparent,
                ),
                modifier = Modifier.padding(horizontal = 16.dp).fillMaxWidth(),
            )
        }
        item {
            var menu by remember { mutableStateOf(false) }
            Row(Modifier.padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                Box {
                    FilterChip(
                        selected = here != null, onClick = { menu = true },
                        leadingIcon = { Icon(Icons.Default.LocationOn, null, Modifier.size(18.dp)) },
                        label = { Text(mine.firstOrNull { it.storeId == here }?.name?.let { "I am in $it" } ?: "Where are you?") },
                        trailingIcon = { Icon(Icons.Default.KeyboardArrowDown, null, Modifier.size(18.dp)) },
                    )
                    DropdownMenu(menu, { menu = false }) {
                        DropdownMenuItem(text = { Text("Not in a store") }, onClick = { compare.setHere(null); menu = false })
                        mine.forEach { s -> DropdownMenuItem(text = { Text(s.name) }, onClick = { compare.setHere(s.storeId); menu = false }) }
                    }
                }
            }
        }
        state.error?.let { item { ErrorCard(it, Modifier.padding(horizontal = 16.dp), onRetry = { state.gtin?.let(compare::lookup) }) } }

        if (state.gtin == null) {
            item {
                EmptyState(AppIcons.Swap, "Compare in seconds", if (mine.isEmpty()) "Choose your stores in Settings first, then scan any product's barcode."
                else "Scan a barcode and see the price in each of your ${mine.size} stores, cheapest first.")
            }
        } else {
            item { ProductHeader(state, Modifier.padding(horizontal = 16.dp)) }
            if (state.product == null && !state.loading && !state.checkingLive && state.rows.none { it.status == "found" }) item {
                UnknownBarcodeCard(model, state.gtin!!, Modifier.padding(horizontal = 16.dp))
            }
            if (state.checkingLive || state.loading) item {
                Column(Modifier.padding(horizontal = 20.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    LinearProgressIndicator(Modifier.fillMaxWidth())
                    Text(state.progress ?: "Looking up the barcode…", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            val rows = sortRows(state.rows)
            items(rows, key = { it.storeId }) { row -> CompareRowCard(row, model, here == row.storeId, Modifier.padding(horizontal = 16.dp)) }
        }
    }
}

/** Rows with a price first, cheapest first by what the person pays (deals count), then the stores that do not have it or were not asked. */
internal fun sortRows(rows: List<CompareRow>): List<CompareRow> =
    rows.sortedWith(compareBy({ (it.effectivePrice ?: it.currentPrice).toAmount() == null }, { (it.effectivePrice ?: it.currentPrice).toAmount() ?: 0.0 }, { it.storeName }))

@Composable
private fun ProductHeader(state: app.pricewatch.ui.CompareModel.State, modifier: Modifier) {
    val product = state.product
    val cheapest = state.cheapest
    Column(modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(product?.name ?: "Barcode ${state.gtin?.trimStart('0')}", style = MaterialTheme.typography.titleLarge)
        if (product == null && !state.loading) Text("This barcode is not in the catalogue yet. The stores are being asked.", color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodyMedium)
        if (cheapest != null && (cheapest.effectivePrice ?: cheapest.currentPrice) != null && !state.checkingLive) {
            val saving = state.saving?.amount.toAmount()
            Surface2(
                if (saving != null && saving < 0) "Cheaper elsewhere: save ${formatMoney(-saving, "SEK")}${state.saving?.percent?.toAmount()?.let { " (${(-it).toInt()}%)" } ?: ""} at ${cheapest.storeName}"
                else if (saving != null && saving >= 0 && state.rows.size > 1) "Best price is here: ${cheapest.storeName} ${formatMoney(cheapest.effectivePrice ?: cheapest.currentPrice)}"
                else "Cheapest: ${cheapest.storeName} ${formatMoney(cheapest.effectivePrice ?: cheapest.currentPrice)}",
            )
        }
    }
}

@Composable
private fun Surface2(text: String) {
    Row(
        Modifier.fillMaxWidth().clip(MaterialTheme.shapes.medium).background(priceColors.cheaperContainer).padding(14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) { Text(text, color = priceColors.cheaper, style = MaterialTheme.typography.titleSmall) }
}

@Composable
private fun CompareRowCard(row: CompareRow, model: AppModel, isHere: Boolean, modifier: Modifier) {
    val found = row.status == "found" && row.currentPrice != null
    val muted = MaterialTheme.colorScheme.onSurfaceVariant
    SoftCard(modifier.fillMaxWidth(), onClick = row.sourceUrl?.takeIf { found }?.let { url -> { model.platform.openUrl(url) } }) {
        Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
            RetailerBadge(model.directory.retailerName(row.retailerId), model.directory.logoUrl(row.retailerId), 36.dp)
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                Text(row.storeName, style = MaterialTheme.typography.titleSmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
                if (found) {
                    row.best?.let { Text(dealNote(it), style = MaterialTheme.typography.bodySmall, color = priceColors.offer) }
                    val sub = listOfNotNull(
                        if (row.best != null) "shelf ${formatMoney(row.currentPrice, row.currency)}" else null,
                        formatUnitPrice(row.unitPrice?.amount, row.unitPrice?.unit, row.currency),
                        if (row.availability == "out_of_stock") "Out of stock" else null,
                        row.ageSeconds?.takeIf { row.source == "catalogue" }?.let { ageWords(it) },
                    ).joinToString(" · ")
                    if (sub.isNotEmpty()) Text(sub, style = MaterialTheme.typography.bodySmall, color = muted)
                } else Text(statusWords(row), style = MaterialTheme.typography.bodySmall, color = muted)
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    if (row.isCheapest && found) Pill("Cheapest", priceColors.cheaperContainer, priceColors.cheaper)
                    if (isHere) Pill("You are here", MaterialTheme.colorScheme.secondaryContainer, MaterialTheme.colorScheme.onSecondaryContainer, icon = Icons.Default.LocationOn)
                }
            }
            if (found) {
                Column(horizontalAlignment = Alignment.End) {
                    Text(formatMoney(row.effectivePrice ?: row.currentPrice, row.currency), style = MaterialTheme.typography.titleLarge.merge(PriceStyle),
                        color = if (row.isCheapest) priceColors.cheaper else MaterialTheme.colorScheme.onSurface)
                    val extra = row.vsCheapest?.amount.toAmount()
                    if (extra != null && extra > 0.004) Text("+${formatMoney(extra, row.currency)}", style = MaterialTheme.typography.bodySmall, color = priceColors.dearer)
                }
            }
        }
    }
}

private fun ageWords(seconds: Long) = when {
    seconds < 90 -> "just now"
    seconds < 3_600 -> "${seconds / 60} min ago"
    seconds < 86_400 -> "${seconds / 3_600} h ago"
    else -> "${seconds / 86_400} d ago"
}.let { "checked $it" }

private fun statusWords(row: CompareRow) = when (row.status) {
    "not_found" -> "Does not sell this"
    "unknown" -> "Not checked yet"
    "unknown_mapping" -> "Cannot be searched by barcode"
    "paused", "disabled" -> "Switched off for now"
    else -> row.detail ?: row.status.replace('_', ' ')
}

/** Nothing sells this barcode that the service knows of: it may be a product a person already watches without a barcode. */
@Composable
private fun UnknownBarcodeCard(model: AppModel, gtin: String, modifier: Modifier) {
    val monitors by model.monitors.items.collectAsState()
    val candidates = monitors.filter { it.productId != null && it.barcode == null }
    var picking by remember { mutableStateOf(false) }
    SoftCard(modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Not found in your stores", style = MaterialTheme.typography.titleMedium)
            Text(
                if (candidates.isEmpty()) "No store has this barcode on record. Search for the item in Lookup by name, and link the barcode there."
                else "If this is a product you already watch, link the barcode to it. Then it can be compared in every store.",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (candidates.isNotEmpty()) androidx.compose.material3.FilledTonalButton(onClick = { picking = true }) { Text("Link to a product I watch") }
        }
    }
    if (picking) androidx.compose.material3.AlertDialog(
        onDismissRequest = { picking = false },
        title = { Text("Which product is it?") },
        text = {
            LazyColumn {
                items(candidates, key = { it.id }) { m ->
                    Text(
                        m.productName ?: m.name, style = MaterialTheme.typography.bodyLarge,
                        modifier = Modifier.fillMaxWidth().clickable {
                            picking = false
                            model.barcodes.link(m.productId!!, m.productName ?: m.name, gtin) { model.compare.lookup(gtin) }
                        }.padding(vertical = 12.dp),
                    )
                }
            }
        },
        confirmButton = {},
        dismissButton = { androidx.compose.material3.TextButton(onClick = { picking = false }) { Text("Cancel") } },
    )
}
