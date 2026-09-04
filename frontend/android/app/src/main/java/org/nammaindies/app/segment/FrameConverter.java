package org.nammaindies.app.segment;

import androidx.camera.core.ImageProxy;

import java.nio.ByteBuffer;

/**
 * Camera frame to model input: rotate, letterbox, normalise, in one pass.
 *
 * <h3>Why RGBA and not YUV</h3>
 * CameraX can hand back {@code RGBA_8888} directly, and asking for it removes
 * the whole YUV_420_888 conversion — three planes, chroma subsampling, and a
 * pixel-stride quirk that differs by device. That conversion is the classic way
 * a real-time pipeline loses its frame budget before inference even starts. The
 * cost is paid inside CameraX where it is already optimised.
 *
 * <h3>One pass, output-driven</h3>
 * The loop walks the OUTPUT pixels and maps each back to a source pixel, rather
 * than walking the source and writing forward. That makes rotation, scaling and
 * letterbox padding a single index calculation with no intermediate bitmap, no
 * allocation per frame, and no second copy.
 *
 * Sampling is nearest-neighbour. For a 3.13 M-parameter detector at 320px that
 * is not the accuracy bottleneck, and bilinear would cost four samples and
 * three lerps per pixel across ~100k pixels. If this ever needs to be better,
 * the answer is to do it on the GPU, not to make this loop cleverer.
 */
final class FrameConverter {

    /** The grey YOLO letterboxes with, matching the server's `_letterbox`
     *  (114,114,114). Padding with black instead shifts the input distribution
     *  away from what the model was trained and benchmarked on. */
    private static final float PAD = 114f / 255f;

    private FrameConverter() {
    }

    /**
     * Fill {@code out} with a normalised NHWC RGB tensor of edge {@code size}.
     *
     * @return the letterbox mapping, needed to put boxes back on the screen.
     */
    static Letterbox convert(ImageProxy image, int size, ByteBuffer out) {
        ImageProxy.PlaneProxy plane = image.getPlanes()[0];
        ByteBuffer src = plane.getBuffer();
        int rowStride = plane.getRowStride();
        int pixelStride = plane.getPixelStride();

        int srcW = image.getWidth();
        int srcH = image.getHeight();
        int rotation = ((image.getImageInfo().getRotationDegrees() % 360) + 360) % 360;

        // Dimensions after rotation — what the model will effectively see.
        boolean swap = rotation == 90 || rotation == 270;
        int rotW = swap ? srcH : srcW;
        int rotH = swap ? srcW : srcH;

        float scale = Math.min(size / (float) rotW, size / (float) rotH);
        int fitW = Math.max(1, Math.round(rotW * scale));
        int fitH = Math.max(1, Math.round(rotH * scale));
        int padX = (size - fitW) / 2;
        int padY = (size - fitH) / 2;

        out.rewind();
        for (int dy = 0; dy < size; dy++) {
            int ry = dy - padY;
            for (int dx = 0; dx < size; dx++) {
                int rx = dx - padX;
                if (rx < 0 || ry < 0 || rx >= fitW || ry >= fitH) {
                    out.putFloat(PAD);
                    out.putFloat(PAD);
                    out.putFloat(PAD);
                    continue;
                }
                // Undo the fit, then undo the rotation, landing on a source pixel.
                int ux = (int) (rx / scale);
                int uy = (int) (ry / scale);
                if (ux >= rotW) ux = rotW - 1;
                if (uy >= rotH) uy = rotH - 1;

                int sx, sy;
                switch (rotation) {
                    case 90:
                        sx = uy;
                        sy = rotW - 1 - ux;
                        break;
                    case 180:
                        sx = rotW - 1 - ux;
                        sy = rotH - 1 - uy;
                        break;
                    case 270:
                        sx = srcW - 1 - uy;
                        sy = ux;
                        break;
                    default:
                        sx = ux;
                        sy = uy;
                }
                if (sx < 0) sx = 0;
                else if (sx >= srcW) sx = srcW - 1;
                if (sy < 0) sy = 0;
                else if (sy >= srcH) sy = srcH - 1;

                int base = sy * rowStride + sx * pixelStride;
                // & 0xFF because Java bytes are signed and a bright pixel would
                // otherwise arrive negative.
                out.putFloat((src.get(base) & 0xFF) / 255f);
                out.putFloat((src.get(base + 1) & 0xFF) / 255f);
                out.putFloat((src.get(base + 2) & 0xFF) / 255f);
            }
        }
        out.rewind();
        return new Letterbox(scale, padX, padY, rotW, rotH);
    }

    /** How the frame was fitted into the square, so coordinates can come back. */
    static final class Letterbox {
        final float scale;
        final int padX;
        final int padY;
        final int contentW;
        final int contentH;

        Letterbox(float scale, int padX, int padY, int contentW, int contentH) {
            this.scale = scale;
            this.padX = padX;
            this.padY = padY;
            this.contentW = contentW;
            this.contentH = contentH;
        }
    }
}
