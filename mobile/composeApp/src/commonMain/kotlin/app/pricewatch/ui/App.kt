package app.pricewatch.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import app.pricewatch.ui.compare.CompareScreen
import app.pricewatch.ui.components.BarcodeConflictDialog
import app.pricewatch.ui.components.ScannerOverlay
import app.pricewatch.ui.components.EmptyState
import app.pricewatch.ui.lookup.LookupScreen
import app.pricewatch.ui.monitor.MonitorScreen
import app.pricewatch.ui.settings.SettingsScreen
import app.pricewatch.ui.theme.AppIcons
import app.pricewatch.ui.theme.PriceWatchTheme

private fun Tab.icon(): ImageVector = when (this) {
    Tab.Lookup -> Icons.Default.Search
    Tab.Monitor -> AppIcons.Eye
    Tab.Compare -> AppIcons.Swap
    Tab.Settings -> Icons.Default.Settings
}

/** The whole app: four tabs along the bottom (Lookup, Monitor, Compare, Settings). */
@Composable
fun App(model: AppModel) {
    PriceWatchTheme {
        val tab by model.tab.collectAsState()
        val session by model.graph.sessions.session.collectAsState()
        val expired by model.graph.sessions.expired.collectAsState()
        val update by model.settings.update.collectAsState()
        val scan by model.scan.collectAsState()
        val conflict by model.barcodes.conflict.collectAsState()
        val snackbar = remember { SnackbarHostState() }
        LaunchedEffect(model) { model.messages.messages.collect { snackbar.showSnackbar(it) } }

        androidx.compose.foundation.layout.Box {
        Scaffold(
            containerColor = MaterialTheme.colorScheme.background,
            snackbarHost = { SnackbarHost(snackbar) },
            bottomBar = {
                NavigationBar(containerColor = MaterialTheme.colorScheme.surfaceContainerLowest, tonalElevation = androidx.compose.ui.unit.Dp.Hairline) {
                    Tab.entries.forEach { t ->
                        val needsAttention = t == Tab.Settings && (update != null || expired)
                        NavigationBarItem(
                            selected = tab == t,
                            onClick = { model.select(t) },
                            icon = {
                                BadgedBox(badge = { if (needsAttention) Badge() }) { Icon(t.icon(), contentDescription = null) }
                            },
                            label = { Text(t.label) },
                            colors = NavigationBarItemDefaults.colors(indicatorColor = MaterialTheme.colorScheme.primaryContainer),
                        )
                    }
                }
            },
        ) { padding ->
            Box(Modifier.padding(padding)) {
                if (tab != Tab.Settings && session == null) {
                    EmptyState(
                        icon = tab.icon(),
                        title = "Connect to your server",
                        text = "Price Watch works with your own Price Watch server. Sign in once and you are ready to look up prices.",
                        actionLabel = "Connect",
                        onAction = { model.select(Tab.Settings) },
                    )
                } else when (tab) {
                    Tab.Lookup -> LookupScreen(model)
                    Tab.Monitor -> MonitorScreen(model)
                    Tab.Compare -> CompareScreen(model)
                    Tab.Settings -> SettingsScreen(model)
                }
            }
        }
        scan?.let { ScannerOverlay(model, it) }
        conflict?.let { BarcodeConflictDialog(model.barcodes, it) }
        }
    }
}
