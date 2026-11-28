package app.pricewatch.ui.components

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import app.pricewatch.shared.format.formatMoney
import app.pricewatch.shared.format.toAmount
import app.pricewatch.ui.BarcodeModel

/** Turns what a person typed ("17,5") into the decimal string the API wants, or null when it is not a price. */
fun parsePrice(text: String): String? {
    val v = text.trim().replace(',', '.').toAmount() ?: return null
    return if (v > 0) text.trim().replace(',', '.') else null
}

/** Sets, changes or removes the price a person wants an alert at. [current] is the existing alert price, if any. */
@Composable
fun AlertPriceDialog(
    productName: String,
    nowPrice: String?,
    current: String?,
    onDismiss: () -> Unit,
    onSave: (String?) -> Unit,
) {
    var text by remember { mutableStateOf(current?.let { formatMoney(it).removeSuffix(" kr").replace(",00", "") } ?: "") }
    val parsed = parsePrice(text)
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(if (current == null) "Set an alert price" else "Change the alert price") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(productName, style = MaterialTheme.typography.titleMedium)
                if (nowPrice != null) Text("Now ${formatMoney(nowPrice)}", color = MaterialTheme.colorScheme.onSurfaceVariant)
                OutlinedTextField(
                    text, { text = it.filter { c -> c.isDigit() || c == ',' || c == '.' }.take(8) },
                    label = { Text("Alert me at or below (kr)") }, singleLine = true, shape = MaterialTheme.shapes.medium,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                    supportingText = { Text("Deals count too: multi-buys, percentages and, if you are a member, member prices.") },
                    isError = text.isNotEmpty() && parsed == null,
                )
            }
        },
        confirmButton = { Button(onClick = { onSave(parsed) }, enabled = parsed != null) { Text("Save") } },
        dismissButton = {
            Column {
                if (current != null) TextButton(onClick = { onSave(null) }) { Text("Remove alert price", color = MaterialTheme.colorScheme.error) }
                TextButton(onClick = onDismiss) { Text("Cancel") }
            }
        },
    )
}

/** The scanned barcode already belongs to another product: are they the same item? */
@Composable
fun BarcodeConflictDialog(model: BarcodeModel, conflict: BarcodeModel.Conflict) {
    AlertDialog(
        onDismissRequest = model::dismissConflict,
        title = { Text("Same item?") },
        text = {
            Text(
                "That barcode already belongs to \"${conflict.existingName}\". If \"${conflict.productName}\" is the same item, " +
                    "they are joined into one and their price history is kept. This is shared with everyone, so only do it if you are sure.",
            )
        },
        confirmButton = { Button(onClick = model::confirmMerge) { Text("Yes, same item") } },
        dismissButton = { TextButton(onClick = model::dismissConflict) { Text("No") } },
    )
}
