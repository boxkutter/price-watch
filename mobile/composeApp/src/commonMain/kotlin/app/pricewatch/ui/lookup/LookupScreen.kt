package app.pricewatch.ui.lookup

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Clear
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.pricewatch.shared.api.SearchItem
import app.pricewatch.shared.format.formatMoney
import app.pricewatch.shared.format.shortDate
import app.pricewatch.shared.format.toAmount
import app.pricewatch.shared.lookup.ProductGroup
import app.pricewatch.ui.AppModel
import app.pricewatch.ui.ScanPurpose
import app.pricewatch.ui.Tab
import app.pricewatch.ui.components.EmptyState
import app.pricewatch.ui.components.dealLabel
import app.pricewatch.ui.components.dealNote
import app.pricewatch.ui.components.parsePrice
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
fun LookupScreen(model: AppModel) {
    val lookup = model.lookup
    val query by lookup.query.collectAsState()
    val busy by lookup.busy.collectAsState()
    val progress by lookup.progress.collectAsState()
    val results by lookup.results.collectAsState()
    val problems by lookup.problems.collectAsState()
    val error by lookup.error.collectAsState()
    val linkUrl by lookup.linkUrl.collectAsState()
    val mine by model.directory.mine.collectAsState()
    val storeScope by lookup.storeScope.collectAsState()
    val monitors by model.monitors.items.collectAsState()
    val watched = remember(monitors) { model.monitors.watchedListingIds() }

    var openKey by remember { mutableStateOf<String?>(null) }
    var toWatch by remember { mutableStateOf<SearchItem?>(null) }
    var pickingStores by remember { mutableStateOf(false) }

    LazyColumn(contentPadding = PaddingValues(bottom = 24.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        item { ScreenTitle("Lookup") }
        item {
            SearchField(
                query, onChange = lookup::setQuery, onSearch = { lookup.search() }, onClear = lookup::clearQuery,
                modifier = Modifier.padding(horizontal = 16.dp),
            )
        }
        item {
            val searching = if (storeScope.isEmpty()) mine.size else storeScope.count { id -> mine.any { it.storeId == id } }
            Row(Modifier.padding(horizontal = 16.dp), verticalAlignment = Alignment.CenterVertically) {
                FilterChip(
                    selected = false, onClick = { pickingStores = true },
                    label = {
                        Text(
                            when {
                                mine.isEmpty() -> "No stores chosen"
                                storeScope.isEmpty() -> "All my stores ($searching)"
                                else -> "$searching of ${mine.size} stores"
                            },
                        )
                    },
                    trailingIcon = { Icon(Icons.Default.KeyboardArrowDown, null, Modifier.size(18.dp)) },
                )
                Spacer(Modifier.weight(1f))
                if (results != null && !busy) TextButton(onClick = { lookup.search(fresh = true) }) { Text("Refresh prices") }
            }
        }
        if (busy) item {
            Column(Modifier.padding(horizontal = 20.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                LinearProgressIndicator(Modifier.fillMaxWidth())
                progress?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
            }
        }
        error?.let { item { ErrorCard(it, Modifier.padding(horizontal = 16.dp), onDismiss = lookup::dismissError) } }

        linkUrl?.let { url ->
            item { LinkCard(url, model, Modifier.padding(horizontal = 16.dp)) }
        }

        val groups = results
        if (groups == null && linkUrl == null) item {
            EmptyState(
                icon = Icons.Default.Search,
                title = "Find the best price",
                text = if (mine.isEmpty()) "Choose your stores in Settings, then search by product name, barcode or a link to a product page."
                else "Search by product name or barcode, or paste a link to a product page to watch it.",
                actionLabel = if (mine.isEmpty()) "Choose stores" else null,
                onAction = if (mine.isEmpty()) ({ model.select(Tab.Settings) }) else null,
            )
        }
        if (groups != null) {
            if (groups.isEmpty() && !busy && error == null) item {
                EmptyState(Icons.Default.Search, "Nothing found", "No store listed \"${query.trim()}\". Try fewer or different words, or a barcode.")
            }
            items(groups, key = { it.key }) { group ->
                ProductCard(group, model, group.offers.any { it.listingId in watched }, Modifier.padding(horizontal = 16.dp)) { openKey = group.key }
            }
            if (problems.isNotEmpty()) item {
                Column(Modifier.padding(horizontal = 24.dp, vertical = 4.dp)) {
                    problems.forEach { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
                }
            }
        }
    }

    val open = results?.firstOrNull { it.key == openKey }
    if (open != null) {
        GroupSheet(open, model, watched, onWatch = { toWatch = it }, onDismiss = { openKey = null })
    }
    toWatch?.let { item ->
        WatchDialog(
            title = item.displayName ?: item.name,
            store = mine.firstOrNull { it.storeId == item.storeId }?.name,
            currentPrice = item.currentPrice?.let { formatMoney(it, item.currency) },
            onDismiss = { toWatch = null },
            onConfirm = { target -> lookup.watch(item, target); toWatch = null },
        )
    }
    if (pickingStores) {
        StorePickerDialog(model, storeScope, onDismiss = { pickingStores = false }, onChoose = { lookup.setScope(it); pickingStores = false })
    }
}

@Composable
private fun SearchField(value: String, onChange: (String) -> Unit, onSearch: () -> Unit, onClear: () -> Unit, modifier: Modifier = Modifier) {
    val focus = LocalFocusManager.current
    val keyboard = LocalSoftwareKeyboardController.current
    TextField(
        value = value, onValueChange = onChange, singleLine = true,
        placeholder = { Text("Product, barcode or store link") },
        leadingIcon = { Icon(Icons.Default.Search, null) },
        trailingIcon = { if (value.isNotEmpty()) IconButton(onClick = onClear) { Icon(Icons.Default.Clear, "Clear") } },
        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
        keyboardActions = KeyboardActions(onSearch = { keyboard?.hide(); focus.clearFocus(); onSearch() }),
        shape = MaterialTheme.shapes.extraLarge,
        colors = TextFieldDefaults.colors(
            focusedContainerColor = MaterialTheme.colorScheme.surfaceContainerLowest,
            unfocusedContainerColor = MaterialTheme.colorScheme.surfaceContainerLowest,
            focusedIndicatorColor = Color.Transparent, unfocusedIndicatorColor = Color.Transparent,
        ),
        modifier = modifier.fillMaxWidth(),
    )
}

@Composable
private fun ProductCard(group: ProductGroup, model: AppModel, watching: Boolean, modifier: Modifier, onClick: () -> Unit) {
    val cheapest = group.cheapest
    val retailer = model.directory.mine.collectAsState().value.firstOrNull { it.storeId == cheapest.storeId }
    SoftCard(modifier.fillMaxWidth(), onClick = onClick) {
        Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
            ProductImage(model.graph.api.imageUrl(group.image), 68.dp)
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text(group.title, style = MaterialTheme.typography.titleMedium, maxLines = 2, overflow = TextOverflow.Ellipsis)
                if (group.subtitle.isNotEmpty()) Text(group.subtitle, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1)
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 4.dp)) {
                    RetailerBadge(model.directory.retailerName(retailer?.retailerId), model.directory.logoUrl(retailer?.retailerId), 18.dp)
                    Spacer(Modifier.width(6.dp))
                    Text(retailer?.name ?: "", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
            }
            Spacer(Modifier.width(8.dp))
            Column(horizontalAlignment = Alignment.End, verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(formatMoney(cheapest.effectivePrice, cheapest.currency), style = MaterialTheme.typography.titleLarge.merge(PriceStyle))
                if (cheapest.best != null) Text("Shelf ${formatMoney(cheapest.currentPrice, cheapest.currency)}", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (group.storeCount > 1) Pill("${group.storeCount} stores", MaterialTheme.colorScheme.secondaryContainer, MaterialTheme.colorScheme.onSecondaryContainer)
                val best = cheapest.best
                if (best != null) Pill(dealLabel(best), priceColors.cheaperContainer, priceColors.cheaper)
                else if (cheapest.offers.isNotEmpty()) Pill("Offer", MaterialTheme.colorScheme.secondaryContainer, MaterialTheme.colorScheme.onSecondaryContainer)
                if (watching) Pill("Watching", MaterialTheme.colorScheme.primaryContainer, MaterialTheme.colorScheme.onPrimaryContainer, icon = Icons.Default.Check)
            }
        }
    }
}

@Composable
private fun GroupSheet(group: ProductGroup, model: AppModel, watched: Set<String>, onWatch: (SearchItem) -> Unit, onDismiss: () -> Unit) {
    val mine by model.directory.mine.collectAsState()
    val linked by model.barcodes.linked.collectAsState()
    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true), containerColor = MaterialTheme.colorScheme.surface) {
        LazyColumn(contentPadding = PaddingValues(start = 20.dp, end = 20.dp, bottom = 32.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            item {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    ProductImage(model.graph.api.imageUrl(group.image), 72.dp)
                    Spacer(Modifier.width(14.dp))
                    Column {
                        Text(group.title, style = MaterialTheme.typography.titleLarge, maxLines = 3)
                        if (group.subtitle.isNotEmpty()) Text(group.subtitle, color = MaterialTheme.colorScheme.onSurfaceVariant)
                        group.cheapest.barcode?.let { Text(it.gtin14.trimStart('0'), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.outline) }
                    }
                }
            }
            item { Spacer(Modifier.size(4.dp)) }
            items(group.offers, key = { it.listingId + it.storeId }) { item ->
                val store = mine.firstOrNull { it.storeId == item.storeId }
                val lowest = group.cheapest.currentPrice.toAmount()
                val isCheapest = group.offers.size > 1 && lowest != null && item.currentPrice.toAmount() == lowest
                SoftCard(Modifier.fillMaxWidth()) {
                    Row(Modifier.padding(start = 14.dp, top = 10.dp, bottom = 10.dp, end = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                        RetailerBadge(model.directory.retailerName(store?.retailerId), model.directory.logoUrl(store?.retailerId), 32.dp)
                        Spacer(Modifier.width(12.dp))
                        Column(Modifier.weight(1f)) {
                            Text(store?.name ?: "Store", style = MaterialTheme.typography.titleSmall, maxLines = 1, overflow = TextOverflow.Ellipsis)
                            val best = item.best
                            val offer = item.offers.firstOrNull()
                            val note = when {
                                best != null -> listOfNotNull(dealNote(best), "shelf ${formatMoney(item.currentPrice, item.currency)}", best.validTo?.let { "until ${shortDate(it)}" }).joinToString(" · ")
                                offer != null -> listOfNotNull(
                                    offer.terms,
                                    offer.unitPrice?.let { "${formatMoney(it, item.currency)} each" },
                                    if (offer.eligibility == "member") "members only" else null,
                                    offer.validTo?.let { "until ${shortDate(it)}" },
                                ).joinToString(" · ")
                                item.availability == "out_of_stock" -> "Out of stock"
                                else -> null
                            }
                            if (note != null) Text(note, style = MaterialTheme.typography.bodySmall, color = if (best != null || offer != null) priceColors.offer else MaterialTheme.colorScheme.onSurfaceVariant)
                            if (isCheapest) Pill("Cheapest", priceColors.cheaperContainer, priceColors.cheaper, Modifier.padding(top = 4.dp))
                        }
                        Spacer(Modifier.width(8.dp))
                        Text(formatMoney(item.effectivePrice, item.currency), style = MaterialTheme.typography.titleMedium.merge(PriceStyle))
                        Spacer(Modifier.width(6.dp))
                        if (item.listingId in watched) {
                            Icon(Icons.Default.Check, "Watching", Modifier.padding(12.dp).size(24.dp), tint = MaterialTheme.colorScheme.primary)
                        } else {
                            FilledTonalButton(onClick = { onWatch(item) }, contentPadding = PaddingValues(horizontal = 12.dp)) {
                                Icon(Icons.Default.Add, null, Modifier.size(18.dp)); Spacer(Modifier.width(4.dp)); Text("Watch")
                            }
                        }
                    }
                }
            }
            if (group.cheapest.barcode == null && group.cheapest.productId !in linked) item {
                Row(Modifier.fillMaxWidth().padding(top = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text("No barcode known for this item.", Modifier.weight(1f), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    TextButton(onClick = {
                        onDismiss()
                        model.startScan(ScanPurpose.LinkBarcode(group.cheapest.productId, group.title))
                    }) { Icon(AppIcons.QrScan, null, Modifier.size(18.dp)); Spacer(Modifier.width(6.dp)); Text("Scan to link") }
                }
            }
            group.spread?.let { spread ->
                item { Text("Up to ${formatMoney(spread, group.cheapest.currency)} cheaper than the dearest store.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(top = 4.dp)) }
            }
        }
    }
}

@Composable
internal fun WatchDialog(title: String, store: String?, currentPrice: String?, onDismiss: () -> Unit, onConfirm: (String?) -> Unit) {
    var target by remember { mutableStateOf("") }
    val parsed = parsePrice(target)
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Watch this product") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(title, style = MaterialTheme.typography.titleMedium)
                Text(listOfNotNull(store, currentPrice?.let { "now $it" }).joinToString(" · "), color = MaterialTheme.colorScheme.onSurfaceVariant)
                OutlinedTextField(
                    target, { target = it.filter { c -> c.isDigit() || c == ',' || c == '.' }.take(8) },
                    label = { Text("Alert me at or below (kr)") }, singleLine = true, shape = MaterialTheme.shapes.medium,
                    supportingText = { Text("Optional. You are always told when the price changes.") },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                    isError = target.isNotEmpty() && parsed == null,
                )
            }
        },
        confirmButton = { Button(onClick = { onConfirm(parsed) }, enabled = target.isEmpty() || parsed != null) { Text("Watch") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun StorePickerDialog(model: AppModel, current: Set<String>, onDismiss: () -> Unit, onChoose: (Set<String>) -> Unit) {
    val mine by model.directory.mine.collectAsState()
    var picked by remember { mutableStateOf(current.ifEmpty { mine.map { it.storeId }.toSet() }) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Search in") },
        text = {
            LazyColumn {
                items(mine, key = { it.storeId }) { store ->
                    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(store.storeId in picked, { on -> picked = if (on) picked + store.storeId else picked - store.storeId })
                        RetailerBadge(model.directory.retailerName(store.retailerId), model.directory.logoUrl(store.retailerId), 24.dp)
                        Spacer(Modifier.width(10.dp))
                        Text(store.name, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    }
                }
                if (mine.isEmpty()) item { Text("You have not chosen any stores yet. Do that in Settings.") }
            }
        },
        confirmButton = { Button(onClick = { onChoose(if (picked.size == mine.size) emptySet() else picked) }, enabled = picked.isNotEmpty()) { Text("Done") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun LinkCard(url: String, model: AppModel, modifier: Modifier = Modifier) {
    val mine by model.directory.mine.collectAsState()
    val busy by model.lookup.busy.collectAsState()
    var storeId by remember(mine) { mutableStateOf(mine.singleOrNull()?.storeId) }
    var target by remember { mutableStateOf("") }
    var menu by remember { mutableStateOf(false) }
    SoftCard(modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("Add from a link", style = MaterialTheme.typography.titleMedium)
            Text(url, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 2, overflow = TextOverflow.Ellipsis)
            Box {
                OutlinedTextField(
                    mine.firstOrNull { it.storeId == storeId }?.name ?: "", {}, readOnly = true, label = { Text("Store") },
                    trailingIcon = { Icon(Icons.Default.KeyboardArrowDown, null) }, shape = MaterialTheme.shapes.medium, modifier = Modifier.fillMaxWidth(),
                )
                Box(Modifier.matchParentSize().clickable { menu = true })
                DropdownMenu(menu, { menu = false }) {
                    mine.forEach { s -> DropdownMenuItem(text = { Text(s.name) }, onClick = { storeId = s.storeId; menu = false }) }
                }
            }
            OutlinedTextField(
                target, { target = it.filter { c -> c.isDigit() || c == ',' || c == '.' }.take(8) },
                label = { Text("Alert me at or below (kr), optional") }, singleLine = true, shape = MaterialTheme.shapes.medium,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal), modifier = Modifier.fillMaxWidth(),
            )
            Button(onClick = { model.lookup.watchLink(storeId, parsePrice(target)) }, enabled = !busy && storeId != null, modifier = Modifier.fillMaxWidth()) { Text("Watch this product") }
        }
    }
}
