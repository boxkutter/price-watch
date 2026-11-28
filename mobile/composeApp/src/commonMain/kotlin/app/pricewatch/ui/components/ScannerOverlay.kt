package app.pricewatch.ui.components

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.backhandler.BackHandler
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import app.pricewatch.shared.format.asBarcode
import app.pricewatch.ui.AppModel
import app.pricewatch.ui.ScanPurpose

/** The full-screen camera, for comparing prices or for giving a product its barcode. */
@Composable
fun ScannerOverlay(model: AppModel, purpose: ScanPurpose) {
    BackHandler { model.stopScan() }
    var typing by remember { mutableStateOf(false) }
    Box(Modifier.fillMaxSize().background(Color.Black)) {
        model.platform.BarcodeScanner(Modifier.fillMaxSize(), model::scanned)
        // a framing guide, purely visual
        Box(
            Modifier.align(Alignment.Center).fillMaxWidth(0.78f).height(150.dp).clip(RoundedCornerShape(20.dp))
                .background(Color.White.copy(alpha = 0.08f)),
        )
        TextButton(
            onClick = { typing = true },
            modifier = Modifier.align(Alignment.BottomCenter).windowInsetsPadding(WindowInsets.navigationBars).padding(bottom = 8.dp),
        ) { Text("Type the number instead", color = Color.White) }
        Text(
            when (purpose) {
                ScanPurpose.Compare -> "Point the camera at a barcode"
                is ScanPurpose.LinkBarcode -> "Scan the barcode of\n${purpose.productName}"
            },
            color = Color.White, style = MaterialTheme.typography.titleMedium, textAlign = TextAlign.Center,
            modifier = Modifier.align(Alignment.BottomCenter).windowInsetsPadding(WindowInsets.navigationBars).padding(start = 32.dp, end = 32.dp, bottom = 64.dp),
        )
        IconButton(
            onClick = model::stopScan,
            modifier = Modifier.align(Alignment.TopStart).windowInsetsPadding(WindowInsets.statusBars).padding(16.dp)
                .background(Color.Black.copy(alpha = 0.4f), CircleShape),
        ) { Icon(Icons.Default.Close, "Close", tint = Color.White) }
    }
    if (typing) TypeBarcodeDialog(onDismiss = { typing = false }, onDone = { typing = false; model.scanned(it) })
}

@Composable
private fun TypeBarcodeDialog(onDismiss: () -> Unit, onDone: (String) -> Unit) {
    var digits by remember { mutableStateOf("") }
    val code = asBarcode(digits)
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Type the barcode") },
        text = {
            OutlinedTextField(
                digits, { digits = it.filter { c -> c.isDigit() }.take(14) }, singleLine = true, label = { Text("Number under the bars") },
                shape = MaterialTheme.shapes.medium, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                supportingText = { Text("8, 12, 13 or 14 digits") },
            )
        },
        confirmButton = { Button(onClick = { code?.let(onDone) }, enabled = code != null) { Text("OK") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}
