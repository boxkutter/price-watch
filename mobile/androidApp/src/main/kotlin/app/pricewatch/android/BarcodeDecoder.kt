package app.pricewatch.android

import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.NotFoundException
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer

/** Reads product barcodes (EAN-13, EAN-8, UPC-A, UPC-E) from a camera frame's brightness values. */
class BarcodeDecoder {
    private val reader = MultiFormatReader().apply {
        setHints(
            mapOf(
                DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.EAN_13, BarcodeFormat.EAN_8, BarcodeFormat.UPC_A, BarcodeFormat.UPC_E),
                DecodeHintType.TRY_HARDER to true,
            ),
        )
    }

    /** [luma] holds [width] x [height] brightness values, row by row, upright (already turned by the camera's rotation). */
    fun decode(luma: ByteArray, width: Int, height: Int): String? = try {
        val source = PlanarYUVLuminanceSource(luma, width, height, 0, 0, width, height, false)
        reader.decodeWithState(BinaryBitmap(HybridBinarizer(source))).text
    } catch (_: NotFoundException) {
        null
    } catch (_: com.google.zxing.ReaderException) {
        null
    } finally {
        reader.reset()
    }

    companion object {
        /** Turns a frame clockwise by [degrees] (0, 90, 180 or 270): camera sensors are not upright. */
        fun rotate(data: ByteArray, width: Int, height: Int, degrees: Int): Triple<ByteArray, Int, Int> = when (degrees) {
            90 -> {
                val out = ByteArray(data.size)
                for (y in 0 until height) for (x in 0 until width) out[x * height + (height - 1 - y)] = data[y * width + x]
                Triple(out, height, width)
            }
            180 -> Triple(data.reversedArray(), width, height)
            270 -> {
                val out = ByteArray(data.size)
                for (y in 0 until height) for (x in 0 until width) out[(width - 1 - x) * height + y] = data[y * width + x]
                Triple(out, height, width)
            }
            else -> Triple(data, width, height)
        }
    }
}
