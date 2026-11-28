package app.pricewatch.ui.settings

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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material.icons.filled.KeyboardArrowRight
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TimePicker
import androidx.compose.material3.rememberTimePickerState
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.backhandler.BackHandler
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.pricewatch.shared.notify.QuietHours
import app.pricewatch.shared.session.NotificationPrefs
import app.pricewatch.shared.session.SignIn
import app.pricewatch.ui.AppModel
import app.pricewatch.ui.ConnectState
import app.pricewatch.ui.InstallState
import app.pricewatch.ui.SettingsModel
import app.pricewatch.ui.components.ErrorCard
import app.pricewatch.ui.components.RetailerBadge
import app.pricewatch.ui.components.ScreenTitle
import app.pricewatch.ui.components.SectionLabel
import app.pricewatch.ui.components.SoftCard
import app.pricewatch.ui.theme.AppIcons

@Composable
fun SettingsScreen(model: AppModel) {
    var stores by remember { mutableStateOf(false) }
    if (stores) {
        BackHandler { stores = false }
        StoresPage(model, onBack = { stores = false })
    } else {
        MainPage(model, openStores = { stores = true })
    }
}

@Composable
private fun MainPage(model: AppModel, openStores: () -> Unit) {
    val session by model.graph.sessions.session.collectAsState()
    val expired by model.graph.sessions.expired.collectAsState()
    val update by model.settings.update.collectAsState()
    val mine by model.directory.mine.collectAsState()

    LazyColumn(contentPadding = PaddingValues(bottom = 32.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item { ScreenTitle("Settings") }
        update?.let { item { UpdateCard(model, it, Modifier.padding(horizontal = 16.dp)) } }
        if (expired) item {
            ErrorCard("Your sign-in has expired. Sign in again to keep watching prices.", Modifier.padding(horizontal = 16.dp))
        }
        item { SectionLabel("Account") }
        item {
            if (session == null || expired) ConnectCard(model, Modifier.padding(horizontal = 16.dp))
            else AccountCard(model, session!!.email, session!!.baseUrl, Modifier.padding(horizontal = 16.dp))
        }
        if (session != null && !expired) {
            item { SectionLabel("Stores") }
            item {
                SoftCard(Modifier.padding(horizontal = 16.dp).fillMaxWidth(), onClick = openStores) {
                    Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text("My stores", style = MaterialTheme.typography.titleMedium)
                            Text(
                                if (mine.isEmpty()) "Choose where to look for prices" else "${mine.size} chosen · prices are compared across these",
                                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        Icon(Icons.Default.KeyboardArrowRight, null, tint = MaterialTheme.colorScheme.outline)
                    }
                }
            }
            item { SectionLabel("Notifications") }
            item { NotificationsCard(model, Modifier.padding(horizontal = 16.dp)) }
        }
        item { SectionLabel("About") }
        item { AboutCard(model, Modifier.padding(horizontal = 16.dp)) }
    }
}

@Composable
private fun ConnectCard(model: AppModel, modifier: Modifier) {
    val sessions = model.graph.sessions
    val state by model.settings.connect.collectAsState()
    var server by remember { mutableStateOf(sessions.lastServer) }
    var email by remember { mutableStateOf(sessions.lastEmail) }
    var password by remember { mutableStateOf("") }
    var invitation by remember { mutableStateOf("") }
    var joining by remember { mutableStateOf(false) }
    var show by remember { mutableStateOf(false) }
    val busy = state is ConnectState.Busy
    val onConnected = { password = ""; model.select(app.pricewatch.ui.Tab.Lookup) }

    @Composable
    fun Field(value: String, set: (String) -> Unit, label: String, keyboard: KeyboardType = KeyboardType.Text, secret: Boolean = false, hint: String? = null) {
        OutlinedTextField(
            value, { set(it); model.settings.clearConnectError() }, label = { Text(label) }, singleLine = true, enabled = !busy,
            shape = MaterialTheme.shapes.medium, modifier = Modifier.fillMaxWidth(),
            visualTransformation = if (secret && !show) PasswordVisualTransformation() else VisualTransformation.None,
            trailingIcon = if (secret) ({
                IconButton(onClick = { show = !show }) { Icon(if (show) AppIcons.EyeOff else AppIcons.Eye, if (show) "Hide password" else "Show password") }
            }) else null,
            keyboardOptions = KeyboardOptions(keyboardType = if (secret) KeyboardType.Password else keyboard, autoCorrectEnabled = false),
            supportingText = hint?.let { { Text(it) } },
        )
    }

    SoftCard(modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(if (joining) "Join with an invitation" else "Sign in to your server", style = MaterialTheme.typography.titleMedium)
            Text(
                if (joining) "Your administrator gave you an invitation code. Use it to choose your password."
                else "Price Watch runs on your own server. Enter its address and your account.",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Field(server, { server = it }, "Server address", KeyboardType.Uri, hint = "For example 192.168.1.20:8085")
            if (joining) {
                Field(invitation, { invitation = it }, "Invitation code")
                Field(password, { password = it }, "Choose a password", secret = true, hint = "At least ${SignIn.MIN_PASSWORD} characters")
            } else {
                Field(email, { email = it }, "Email", KeyboardType.Email)
                Field(password, { password = it }, "Password", secret = true)
            }
            (state as? ConnectState.Failed)?.let { ErrorCard(it.message) }
            Button(
                onClick = {
                    if (joining) model.settings.join(server, invitation, password, onConnected)
                    else model.settings.connect(server, email, password, onConnected)
                },
                enabled = !busy, modifier = Modifier.fillMaxWidth(),
            ) {
                if (busy) { CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = MaterialTheme.colorScheme.onPrimary); Spacer(Modifier.width(10.dp)) }
                Text(if (busy) "Please wait…" else if (joining) "Create account" else "Sign in")
            }
            TextButton(onClick = { joining = !joining; model.settings.clearConnectError() }, enabled = !busy, modifier = Modifier.align(Alignment.CenterHorizontally)) {
                Text(if (joining) "I already have an account" else "I have an invitation code")
            }
        }
    }
}

@Composable
private fun AccountCard(model: AppModel, email: String, server: String, modifier: Modifier) {
    var confirm by remember { mutableStateOf(false) }
    SoftCard(modifier.fillMaxWidth()) {
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(email, style = MaterialTheme.typography.titleMedium, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(server.removePrefix("http://").removePrefix("https://"), style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            TextButton(onClick = { confirm = true }) { Text("Sign out") }
        }
    }
    if (confirm) AlertDialog(
        onDismissRequest = { confirm = false },
        title = { Text("Sign out?") },
        text = { Text("You will stop getting notifications on this phone until you sign in again. Your watchlist stays on the server.") },
        confirmButton = { Button(onClick = { confirm = false; model.settings.signOut { model.select(app.pricewatch.ui.Tab.Settings) } }) { Text("Sign out") } },
        dismissButton = { TextButton(onClick = { confirm = false }) { Text("Cancel") } },
    )
}

@Composable
private fun NotificationsCard(model: AppModel, modifier: Modifier) {
    val prefs by model.graph.sessions.prefs.collectAsState()
    var allowed by remember { mutableStateOf(model.platform.notificationsAllowed()) }
    var pickingQuiet by remember { mutableStateOf<QuietEnd?>(null) }
    val save = model.settings::savePrefs
    SoftCard(modifier.fillMaxWidth()) {
        Column(Modifier.padding(vertical = 8.dp)) {
            SwitchRow("Price alerts", "Tell me when a watched product changes price", prefs.enabled && allowed) { on ->
                if (on && !allowed) model.platform.requestNotificationPermission { ok ->
                    allowed = ok
                    if (ok) save(prefs.copy(enabled = true)) else model.messages.say("Allow notifications for Price Watch in the system settings")
                } else save(prefs.copy(enabled = on))
            }
            if (!allowed) Row(Modifier.padding(horizontal = 16.dp, vertical = 4.dp), verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Warning, null, Modifier.size(18.dp), tint = MaterialTheme.colorScheme.tertiary)
                Text("Notifications are blocked for this app.", Modifier.weight(1f).padding(horizontal = 8.dp), style = MaterialTheme.typography.bodySmall)
                TextButton(onClick = model.platform::openNotificationSettings) { Text("Open settings") }
            }
            if (prefs.enabled && allowed) {
                HorizontalDivider(Modifier.padding(horizontal = 16.dp), color = MaterialTheme.colorScheme.outlineVariant)
                SwitchRow("Price drops and targets", null, prefs.priceDrops) { save(prefs.copy(priceDrops = it)) }
                SwitchRow("Offers", null, prefs.offers) { save(prefs.copy(offers = it)) }
                SwitchRow("Back in stock", null, prefs.stock) { save(prefs.copy(stock = it)) }
                SwitchRow("Price increases", null, prefs.priceIncreases) { save(prefs.copy(priceIncreases = it)) }
                HorizontalDivider(Modifier.padding(horizontal = 16.dp), color = MaterialTheme.colorScheme.outlineVariant)
                SwitchRow(
                    "Quiet hours",
                    if (prefs.quietEnabled) "${QuietHours.label(prefs.quietStart)} to ${QuietHours.label(prefs.quietEnd)}: alerts wait"
                    else "Hold alerts back overnight",
                    prefs.quietEnabled,
                ) { save(prefs.copy(quietEnabled = it)) }
                if (prefs.quietEnabled) Row(Modifier.padding(horizontal = 16.dp, vertical = 2.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedButton(onClick = { pickingQuiet = QuietEnd.From }) { Text("From ${QuietHours.label(prefs.quietStart)}") }
                    OutlinedButton(onClick = { pickingQuiet = QuietEnd.To }) { Text("To ${QuietHours.label(prefs.quietEnd)}") }
                }
                HorizontalDivider(Modifier.padding(horizontal = 16.dp, vertical = 4.dp), color = MaterialTheme.colorScheme.outlineVariant)
                Column(Modifier.padding(horizontal = 16.dp, vertical = 10.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("Look for changes", style = MaterialTheme.typography.bodyLarge)
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        NotificationPrefs.intervals.forEach { m ->
                            FilterChip(prefs.intervalMinutes == m, { save(prefs.copy(intervalMinutes = m)) }, label = { Text(intervalLabel(m)) })
                        }
                    }
                    Text("The server checks each product about once an hour; this is how often the phone asks the server.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                TextButton(onClick = model.settings::checkEventsNow, Modifier.padding(horizontal = 8.dp)) { Text("Check for alerts now") }
            }
        }
    }
    pickingQuiet?.let { which ->
        QuietTimeDialog(
            if (which == QuietEnd.From) "Quiet from" else "Quiet until",
            if (which == QuietEnd.From) prefs.quietStart else prefs.quietEnd,
            onDismiss = { pickingQuiet = null },
            onPick = { m -> pickingQuiet = null; save(if (which == QuietEnd.From) prefs.copy(quietStart = m) else prefs.copy(quietEnd = m)) },
        )
    }
}


private enum class QuietEnd { From, To }

@Composable
private fun QuietTimeDialog(title: String, minutes: Int, onDismiss: () -> Unit, onPick: (Int) -> Unit) {
    val state = rememberTimePickerState(initialHour = minutes / 60, initialMinute = minutes % 60, is24Hour = true)
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = { TimePicker(state) },
        confirmButton = { Button(onClick = { onPick(state.hour * 60 + state.minute) }) { Text("OK") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

private fun intervalLabel(minutes: Int) = if (minutes < 60) "$minutes min" else "${minutes / 60} h"

@Composable
private fun SwitchRow(title: String, subtitle: String?, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().clickable { onChange(!checked) }.padding(horizontal = 16.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            subtitle?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
        }
        Switch(checked, onChange)
    }
}

@Composable
private fun UpdateCard(model: AppModel, update: app.pricewatch.shared.session.AppUpdate, modifier: Modifier) {
    val install by model.settings.install.collectAsState()
    Surface(modifier.fillMaxWidth(), shape = MaterialTheme.shapes.large, color = MaterialTheme.colorScheme.primaryContainer) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(AppIcons.Download, null, tint = MaterialTheme.colorScheme.onPrimaryContainer)
                Spacer(Modifier.width(10.dp))
                Text("Update available: ${update.release.versionName}", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.onPrimaryContainer)
            }
            if (update.release.notes.isNotBlank()) Text(update.release.notes, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onPrimaryContainer)
            when (val s = install) {
                is InstallState.Downloading -> {
                    if (s.percent != null) LinearProgressIndicator(progress = { s.percent / 100f }, Modifier.fillMaxWidth())
                    else LinearProgressIndicator(Modifier.fillMaxWidth())
                    Text("Downloading…", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onPrimaryContainer)
                }
                is InstallState.Failed -> {
                    Text(s.message, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.error)
                    Button(onClick = model.settings::installUpdate) { Text("Try again") }
                }
                InstallState.Idle -> Button(onClick = model.settings::installUpdate) { Text("Install update") }
            }
        }
    }
}

@Composable
private fun AboutCard(model: AppModel, modifier: Modifier) {
    val checking by model.settings.checkingUpdate.collectAsState()
    val update by model.settings.update.collectAsState()
    SoftCard(modifier.fillMaxWidth()) {
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("Price Watch", style = MaterialTheme.typography.titleMedium)
                Text("Version ${model.platform.appVersionName}", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (update == null) OutlinedButton(onClick = { model.settings.checkForUpdate(announce = true) }, enabled = !checking) {
                if (checking) CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp) else Text("Check for updates")
            }
        }
    }
}

@Composable
private fun StoresPage(model: AppModel, onBack: () -> Unit) {
    val all by model.directory.stores.collectAsState()
    val mine by model.directory.mine.collectAsState()
    val found by model.directory.found.collectAsState()
    val connectors by model.directory.connectors.collectAsState()
    val members by model.directory.members.collectAsState()
    val grouped = remember(all) { all.groupBy { it.retailerId } }
    val foundStores = remember(all, found) { all.filter { it.id in found } }
    var finding by remember { mutableStateOf(false) }
    val canFind = connectors.values.any { it.canFindStores }

    @Composable
    fun StoreRow(store: app.pricewatch.shared.api.Store) {
        val on = mine.any { it.storeId == store.id }
        Row(
            Modifier.fillMaxWidth().clickable { model.settings.setStore(store, !on) }.padding(horizontal = 12.dp, vertical = 2.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Checkbox(on, { model.settings.setStore(store, it) })
            Text(store.name, Modifier.weight(1f))
        }
    }

    LazyColumn(contentPadding = PaddingValues(bottom = 32.dp)) {
        item {
            Row(Modifier.padding(horizontal = 8.dp, vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") }
                Text("My stores", style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
                Text("${mine.size} chosen", color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(end = 16.dp))
            }
        }
        item {
            Text(
                "Lookup and Compare use these stores. Stores of one chain can have different prices, so choose the ones you really shop at.",
                style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(horizontal = 20.dp, vertical = 4.dp),
            )
        }
        if (canFind) item {
            OutlinedButton(onClick = { model.settings.resetFind(); finding = true }, Modifier.padding(horizontal = 20.dp, vertical = 8.dp)) {
                Icon(Icons.Default.Search, null, Modifier.size(18.dp)); Spacer(Modifier.width(8.dp)); Text("Find stores near me")
            }
        }
        if (foundStores.isNotEmpty()) {
            item(key = "found") {
                Column(Modifier.padding(start = 20.dp, top = 14.dp, bottom = 4.dp, end = 20.dp)) {
                    Text("Found near you", style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
                    Text("Tick the ones you shop at.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            items(foundStores, key = { "f-" + it.id }) { StoreRow(it) }
        }
        if (all.isEmpty()) item { Text("No stores are available on the server yet.", Modifier.padding(20.dp), color = MaterialTheme.colorScheme.onSurfaceVariant) }
        grouped.forEach { (retailerId, stores) ->
            item(key = "h-$retailerId") {
                Row(Modifier.padding(start = 20.dp, end = 20.dp, top = 18.dp, bottom = 2.dp), verticalAlignment = Alignment.CenterVertically) {
                    RetailerBadge(model.directory.retailerName(retailerId), model.directory.logoUrl(retailerId), 26.dp)
                    Spacer(Modifier.width(10.dp))
                    Column(Modifier.weight(1f)) {
                        Text(model.directory.retailerName(retailerId), style = MaterialTheme.typography.titleMedium)
                        Text("I am a member", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Switch(retailerId in members, { model.settings.setMember(retailerId, it) })
                }
            }
            items(stores, key = { it.id }) { StoreRow(it) }
        }
    }
    if (finding) FindStoresDialog(model, onClose = { finding = false })
}

@Composable
private fun FindStoresDialog(model: AppModel, onClose: () -> Unit) {
    val retailers = remember { model.directory.findable() }
    val state by model.settings.find.collectAsState()
    var retailer by remember { mutableStateOf(retailers.firstOrNull()) }
    var menu by remember { mutableStateOf(false) }
    val values = remember { mutableStateMapOf<String, String>() }
    val busy = state is SettingsModel.FindState.Busy
    val inputs = retailer?.storeDiscoveryInputs.orEmpty()
    fun valid(input: app.pricewatch.shared.api.DiscoveryInput): Boolean {
        val v = values[input.name].orEmpty().trim()
        if (v.isEmpty()) return !input.required
        return input.pattern?.let { runCatching { Regex(it).matches(v.replace(" ", "")) || Regex(it).matches(v) }.getOrDefault(true) } ?: true
    }
    val ready = retailer != null && inputs.all(::valid) && !busy

    LaunchedEffect(state) { if (state is SettingsModel.FindState.Done) onClose() }

    AlertDialog(
        onDismissRequest = { if (!busy) onClose() },
        title = { Text("Find stores near me") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("Pick a chain and tell it where you are. It lists its stores nearby.", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Box {
                    OutlinedTextField(
                        retailer?.name ?: "", {}, readOnly = true, label = { Text("Chain") },
                        trailingIcon = { Icon(Icons.Default.KeyboardArrowDown, null) }, shape = MaterialTheme.shapes.medium, modifier = Modifier.fillMaxWidth(),
                    )
                    Box(Modifier.matchParentSize().clickable(enabled = !busy) { menu = true })
                    DropdownMenu(menu, { menu = false }) {
                        retailers.forEach { r -> DropdownMenuItem(text = { Text(r.name) }, onClick = { retailer = r; values.clear(); menu = false }) }
                    }
                }
                inputs.forEach { input ->
                    OutlinedTextField(
                        values[input.name].orEmpty(), { values[input.name] = it; model.settings.resetFind() },
                        label = { Text(input.label.ifBlank { input.name }) }, singleLine = true, enabled = !busy,
                        shape = MaterialTheme.shapes.medium, modifier = Modifier.fillMaxWidth(), isError = !valid(input),
                    )
                }
                (state as? SettingsModel.FindState.Failed)?.let { ErrorCard(it.message) }
                if (busy) Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                    Spacer(Modifier.width(10.dp)); Text("Asking the store…", style = MaterialTheme.typography.bodyMedium)
                }
            }
        },
        confirmButton = {
            Button(onClick = { retailer?.let { model.settings.findStores(it.id, values.filterValues { v -> v.isNotBlank() }.mapValues { (_, v) -> v.trim() }) } }, enabled = ready) { Text("Find stores") }
        },
        dismissButton = { TextButton(onClick = onClose, enabled = !busy) { Text("Cancel") } },
    )
}
