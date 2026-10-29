package io.dlne.chat.data

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * EXIF orientation is applied with Android's own graphics code, which a unit test cannot run, so
 * the geometry lives in [PhotoOrientation] and is checked here against the EXIF specification
 * itself, written out below as it reads, not against the table the code uses.
 */
class PhotoOrientationTest {
    private enum class Side { TOP, BOTTOM, LEFT, RIGHT }

    /**
     * EXIF 2.3, tag 0x0112: for each value, where the stored picture's 0th row and 0th column are
     * when the picture is shown the right way up.
     */
    private val spec = mapOf(
        1 to (Side.TOP to Side.LEFT),
        2 to (Side.TOP to Side.RIGHT),
        3 to (Side.BOTTOM to Side.RIGHT),
        4 to (Side.BOTTOM to Side.LEFT),
        5 to (Side.LEFT to Side.TOP),
        6 to (Side.RIGHT to Side.TOP),
        7 to (Side.RIGHT to Side.BOTTOM),
        8 to (Side.LEFT to Side.BOTTOM),
    )

    /** Where stored pixel (x = column, y = row) of a width x height picture is, shown upright, by the spec's two sentences. */
    private fun shown(orientation: Int, x: Double, y: Double, width: Double, height: Double): Pair<Double, Double> {
        val (row0, column0) = spec.getValue(orientation)
        return if (row0 == Side.TOP || row0 == Side.BOTTOM) {
            // Rows run down the picture and columns across it.
            val across = if (column0 == Side.LEFT) x else width - x
            val down = if (row0 == Side.TOP) y else height - y
            across to down
        } else {
            // On its side: rows run across the upright picture and columns down it.
            val across = if (row0 == Side.LEFT) y else height - y
            val down = if (column0 == Side.TOP) x else width - x
            across to down
        }
    }

    private val width = 40
    private val height = 24 // not square, so mixing up the two sides shows

    private val probes = listOf(
        0.0 to 0.0, width.toDouble() to 0.0, 0.0 to height.toDouble(), width.toDouble() to height.toDouble(), // corners
        10.0 to 5.0, 33.0 to 17.0, 20.0 to 12.0,
    )

    @Test
    fun `every orientation puts every pixel where the EXIF specification says`() {
        for (orientation in 1..8) {
            val upright = PhotoOrientation.uprightSize(orientation, width, height)
            val map = PhotoOrientation.transform(orientation, width, height, upright.width, upright.height)
            for ((x, y) in probes) {
                val (wantX, wantY) = shown(orientation, x, y, width.toDouble(), height.toDouble())
                assertEquals("orientation $orientation, X of ($x, $y)", wantX, map.mapX(x, y), 1e-9)
                assertEquals("orientation $orientation, Y of ($x, $y)", wantY, map.mapY(x, y), 1e-9)
            }
        }
    }

    @Test
    fun `the upright picture is where the picture lands, scaled`() {
        // Half size: every stored pixel lands at half the place the unscaled map gives, inside the scaled picture.
        for (orientation in 1..8) {
            val upright = PhotoOrientation.uprightSize(orientation, width, height)
            val map = PhotoOrientation.transform(orientation, width, height, upright.width / 2, upright.height / 2)
            for ((x, y) in probes) {
                val (wantX, wantY) = shown(orientation, x, y, width.toDouble(), height.toDouble())
                assertEquals("orientation $orientation", wantX / 2, map.mapX(x, y), 1e-9)
                assertEquals("orientation $orientation", wantY / 2, map.mapY(x, y), 1e-9)
                assertTrue(map.mapX(x, y) in 0.0..(upright.width / 2).toDouble())
                assertTrue(map.mapY(x, y) in 0.0..(upright.height / 2).toDouble())
            }
        }
    }

    @Test
    fun `a stretched target scales each side by its own factor`() {
        val map = PhotoOrientation.transform(1, 100, 50, 300, 50) // three times as wide, as tall
        assertEquals(300.0, map.mapX(100.0, 50.0), 1e-9)
        assertEquals(50.0, map.mapY(100.0, 50.0), 1e-9)
    }

    @Test
    fun `orientations 5 to 8 lay the picture on its side, the others do not`() {
        for (orientation in 1..4) {
            assertFalse(PhotoOrientation.swapsSides(orientation))
            assertEquals(PhotoSize(width, height), PhotoOrientation.uprightSize(orientation, width, height))
        }
        for (orientation in 5..8) {
            assertTrue(PhotoOrientation.swapsSides(orientation))
            assertEquals(PhotoSize(height, width), PhotoOrientation.uprightSize(orientation, width, height))
        }
    }

    @Test
    fun `an unknown orientation counts as upright`() {
        for (orientation in listOf(0, 9, -3, 255)) {
            assertFalse(PhotoOrientation.swapsSides(orientation))
            val map = PhotoOrientation.transform(orientation, width, height, width, height)
            assertEquals(12.0, map.mapX(12.0, 7.0), 1e-9)
            assertEquals(7.0, map.mapY(12.0, 7.0), 1e-9)
        }
    }

    @Test
    fun `a phone held upright writes orientation 6, which turns the stored landscape pixels a quarter turn clockwise`() {
        // 40 x 24 stored pixels become a 24 x 40 portrait: the stored top left corner is the shown top right.
        val map = PhotoOrientation.transform(6, width, height, 24, 40)
        assertEquals(24.0, map.mapX(0.0, 0.0), 1e-9)
        assertEquals(0.0, map.mapY(0.0, 0.0), 1e-9)
        // and the matrix is handed to Android row by row: [a b tx / c d ty / 0 0 1]
        assertArrayEquals(floatArrayOf(0f, -1f, 24f, 1f, 0f, 0f, 0f, 0f, 1f), map.matrixValues(), 1e-6f)
    }
}
