package app.pricewatch.android

import com.google.zxing.BarcodeFormat
import com.google.zxing.MultiFormatWriter
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class BarcodeDecoderTest {
    /** A real EAN-13 image: black bars on white, with a quiet margin, as a brightness array. */
    private fun render(code: String, format: BarcodeFormat = BarcodeFormat.EAN_13, scale: Int = 3): Triple<ByteArray, Int, Int> {
        val matrix = MultiFormatWriter().encode(code, format, 190, 60)
        val w = matrix.width * scale + 60
        val h = matrix.height * scale + 60
        val luma = ByteArray(w * h) { 255.toByte() }
        for (y in 0 until matrix.height * scale) for (x in 0 until matrix.width * scale) {
            if (matrix[x / scale, y / scale]) luma[(y + 30) * w + x + 30] = 0
        }
        return Triple(luma, w, h)
    }

    @Test fun readsAnEan13() {
        val (luma, w, h) = render("7310130321085")
        assertEquals("7310130321085", BarcodeDecoder().decode(luma, w, h))
    }

    @Test fun readsAnEan8() {
        val (luma, w, h) = render("73513537", BarcodeFormat.EAN_8)
        assertEquals("73513537", BarcodeDecoder().decode(luma, w, h))
    }

    @Test fun readsABarcodeThatTheSensorDeliveredSideways() {
        val (luma, w, h) = render("7310130321085")
        // the sensor turned the picture a quarter turn; the camera reports 90 degrees so we turn it back
        val (sideways, sw, sh) = BarcodeDecoder.rotate(luma, w, h, 270)
        val (upright, uw, uh) = BarcodeDecoder.rotate(sideways, sw, sh, 90)
        assertEquals("7310130321085", BarcodeDecoder().decode(upright, uw, uh))
        assertEquals(w, uw); assertEquals(h, uh)
    }

    @Test fun anEmptyFrameIsNotABarcode() {
        assertNull(BarcodeDecoder().decode(ByteArray(200 * 100) { 128.toByte() }, 200, 100))
    }
}
