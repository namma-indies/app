package org.nammaindies.app.segment;

import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Matrix;
import android.graphics.Paint;

import androidx.camera.core.ImageProxy;

import java.nio.ByteBuffer;

/**
 * Camera frame to model input: rotate, letterbox, normalise.
 *
 * <h3>Why this goes through Bitmap and Canvas rather than a pixel loop</h3>
 * The obvious implementation walks the output pixels and maps each back to a
 * source pixel — no intermediate bitmap, no allocation, one pass. It is also
 * roughly eight times too slow. Ultralytics measured the preprocessing
 * candidates for this exact model on a live camera path:
 *
 * <pre>
 *   direct ByteBuffer, bilinear        55.6 ms
 *   direct ByteBuffer, nearest         61.1 ms
 *   ByteArray + nearest                10.5 ms
 *   Bitmap + Canvas                     7.4 ms   &lt;- this
 * </pre>
 *
 * At 61 ms the resize alone costs more than the inference it feeds, so a
 * hand-written loop does not merely fail to help, it becomes the bottleneck.
 * {@code Canvas.drawBitmap} with a matrix reaches Skia's native, vectorised
 * scaler instead of the JVM.
 *
 * <h3>Why RGBA and not YUV</h3>
 * CameraX hands back {@code RGBA_8888} directly, which removes the whole
 * YUV_420_888 conversion — three planes, chroma subsampling, and a pixel-stride
 * quirk that differs by device. CameraX does it in libyuv through the NDK. The
 * alternative disqualifies itself: a pure-Java YUV loop measures ~683 ms at
 * 2 MP.
 *
 * Every buffer here is allocated once and reused. Not thread-safe: feed it from
 * the single analysis executor.
 */
final class FrameConverter {

    /** The grey YOLO letterboxes with, matching the server's {@code _letterbox}
     *  (114,114,114). Padding with black instead shifts the input away from the
     *  distribution the model was trained and benchmarked on. */
    private static final int PAD_COLOUR = Color.rgb(114, 114, 114);

    private final int size;
    private final Bitmap dst;
    private final Canvas canvas;
    private final int[] pixels;
    private final Paint paint;
    private final Matrix matrix = new Matrix();

    private Bitmap src;
    private int srcW = -1, srcH = -1;

    FrameConverter(int size) {
        this.size = size;
        this.dst = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
        this.canvas = new Canvas(dst);
        this.pixels = new int[size * size];
        this.paint = new Paint(Paint.FILTER_BITMAP_FLAG);
        // Bilinear on the way down. Nearest measured no faster through Skia and
        // aliases a downscale of 4x or more badly, which costs small-animal
        // detections for nothing.
        this.paint.setAntiAlias(false);
    }

    /**
     * Fill {@code out} with a normalised NHWC RGB tensor of edge {@code size}.
     *
     * @return the letterbox mapping, needed to put boxes back on the screen.
     */
    Letterbox convert(ImageProxy image, ByteBuffer out) {
        int w = image.getWidth();
        int h = image.getHeight();
        if (src == null || srcW != w || srcH != h) {
            if (src != null) src.recycle();
            src = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
            srcW = w;
            srcH = h;
        }
        copyInto(src, image);

        int rotation = ((image.getImageInfo().getRotationDegrees() % 360) + 360) % 360;
        boolean swap = rotation == 90 || rotation == 270;
        int rotW = swap ? h : w;
        int rotH = swap ? w : h;

        float scale = Math.min(size / (float) rotW, size / (float) rotH);
        int fitW = Math.max(1, Math.round(rotW * scale));
        int fitH = Math.max(1, Math.round(rotH * scale));
        float padX = (size - fitW) / 2f;
        float padY = (size - fitH) / 2f;

        // Rotate about the centre, re-centre onto the rotated bounding box,
        // scale to fit, then offset by the padding. Order matters: postScale
        // after the rotation would scale along the wrong axes.
        matrix.reset();
        matrix.postRotate(rotation, w / 2f, h / 2f);
        matrix.postTranslate((rotW - w) / 2f, (rotH - h) / 2f);
        matrix.postScale(scale, scale);
        matrix.postTranslate(padX, padY);

        canvas.drawColor(PAD_COLOUR);
        canvas.drawBitmap(src, matrix, paint);

        dst.getPixels(pixels, 0, size, 0, 0, size, size);
        out.rewind();
        for (int p : pixels) {
            out.putFloat(((p >> 16) & 0xFF) / 255f);
            out.putFloat(((p >> 8) & 0xFF) / 255f);
            out.putFloat((p & 0xFF) / 255f);
        }
        out.rewind();
        return new Letterbox(scale, padX, padY, fitW, fitH);
    }

    /**
     * RGBA plane into an ARGB_8888 bitmap.
     *
     * {@code copyPixelsFromBuffer} needs the buffer to be exactly
     * width*height*4 with no row padding, and CameraX often pads the row
     * stride. The fast path is taken when it does not; otherwise the rows are
     * copied one at a time, which is still native memory movement rather than
     * per-pixel Java.
     */
    private static void copyInto(Bitmap bitmap, ImageProxy image) {
        ImageProxy.PlaneProxy plane = image.getPlanes()[0];
        ByteBuffer buf = plane.getBuffer();
        int rowStride = plane.getRowStride();
        int w = image.getWidth();
        int h = image.getHeight();
        int tight = w * 4;

        buf.rewind();
        if (rowStride == tight) {
            bitmap.copyPixelsFromBuffer(buf);
            return;
        }
        byte[] row = new byte[tight];
        ByteBuffer packed = ByteBuffer.allocate(tight * h);
        for (int y = 0; y < h; y++) {
            buf.position(y * rowStride);
            buf.get(row, 0, tight);
            packed.put(row);
        }
        packed.rewind();
        bitmap.copyPixelsFromBuffer(packed);
    }

    /** How the frame was fitted into the square, so coordinates can come back. */
    static final class Letterbox {
        final float scale;
        final float padX;
        final float padY;
        final int contentW;
        final int contentH;

        Letterbox(float scale, float padX, float padY, int contentW, int contentH) {
            this.scale = scale;
            this.padX = padX;
            this.padY = padY;
            this.contentW = contentW;
            this.contentH = contentH;
        }
    }

    void close() {
        if (src != null) {
            src.recycle();
            src = null;
        }
        dst.recycle();
    }
}
