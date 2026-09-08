package org.nammaindies.app.segment;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.RectF;
import android.view.View;

import java.util.Collections;
import java.util.List;

/**
 * Draws the masks over the camera preview.
 *
 * <h3>Why this is native and not a canvas in the WebView</h3>
 * At 30 fps a mask crossing the JS bridge is roughly 6,400 floats per instance,
 * thirty times a second, serialised through a JSON boundary. That budget does
 * not exist. Only small state — how many animals, how fast, whether the GPU is
 * in use — is sent to JavaScript, at a few hertz. The pixels stay here.
 *
 * <h3>Why each instance gets its own colour</h3>
 * The entire reason this feature exists is that two dogs in one frame currently
 * collapse to one: the server's {@code best_animal_box} keeps the largest box
 * and discards the rest. A single highlight colour would show a person that
 * <em>something</em> was found while still hiding that there are two of them,
 * which is the same failure with a nicer surface. Distinct colours, and a count
 * drawn plainly, are the point.
 */
public final class SegmentOverlay extends View {

    /** Distinct rather than pretty, and chosen to stay separable for the common
     *  forms of colour blindness — this is load-bearing information, not decor. */
    private static final int[] COLOURS = {
            0xFFC65D3B,   // terracotta, the app's own accent
            0xFF2D7A5F,   // green
            0xFF3B6FC6,   // blue
            0xFFC6A63B,   // ochre
    };

    private static final int MASK_ALPHA = 110;

    private final Paint maskPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint boxPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint labelPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint labelBg = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Matrix matrix = new Matrix();

    private List<Segmenter.Instance> instances = Collections.emptyList();
    /** Model input edge, so mask/box coordinates can be mapped to the view. */
    private int inputSize = 1;
    private boolean gpu = true;
    private float fps = 0f;

    public SegmentOverlay(Context context) {
        super(context);
        setWillNotDraw(false);
        boxPaint.setStyle(Paint.Style.STROKE);
        boxPaint.setStrokeWidth(dp(2.5f));
        labelPaint.setColor(Color.WHITE);
        labelPaint.setTextSize(dp(12f));
        labelPaint.setFakeBoldText(true);
        labelBg.setColor(0xB0000000);
        // Nearest-neighbour would show the prototype grid as visible stair-steps;
        // the mask is a soft coverage field, so it should be drawn like one.
        maskPaint.setFilterBitmap(true);
    }

    /** Replace what is drawn. Safe to call from the analysis thread. */
    public void submit(List<Segmenter.Instance> found, int inputSize, boolean gpu, float fps) {
        this.instances = found;
        this.inputSize = Math.max(1, inputSize);
        this.gpu = gpu;
        this.fps = fps;
        postInvalidateOnAnimation();
    }

    public void clear() {
        this.instances = Collections.emptyList();
        postInvalidateOnAnimation();
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        List<Segmenter.Instance> found = instances;

        // The model saw a letterboxed square; the view is not square. Undo the
        // letterbox with the same arithmetic that created it, or every mask sits
        // offset from the animal by the size of the padding — which looks like
        // a tracking bug rather than a coordinate one.
        float viewW = getWidth(), viewH = getHeight();
        float scale = Math.max(viewW, viewH) / (float) inputSize;
        float contentEdge = inputSize * Math.min(viewW, viewH) / Math.max(viewW, viewH);
        float padX = viewW > viewH ? 0 : (inputSize - contentEdge) / 2f;
        float padY = viewW > viewH ? (inputSize - contentEdge) / 2f : 0;

        for (int i = 0; i < found.size(); i++) {
            Segmenter.Instance ins = found.get(i);
            int colour = COLOURS[i % COLOURS.length];

            drawMask(canvas, ins, colour, scale, padX, padY);

            RectF box = new RectF(
                    (ins.box.left - padX) * scale,
                    (ins.box.top - padY) * scale,
                    (ins.box.right - padX) * scale,
                    (ins.box.bottom - padY) * scale);
            boxPaint.setColor(colour);
            canvas.drawRoundRect(box, dp(6f), dp(6f), boxPaint);

            String label = (ins.isDog() ? "DOG " : "CAT ")
                    + Math.round(ins.confidence * 100) + "%";
            float tw = labelPaint.measureText(label);
            float lh = dp(16f);
            canvas.drawRoundRect(box.left, Math.max(0, box.top - lh),
                    box.left + tw + dp(10f), Math.max(lh, box.top),
                    dp(3f), dp(3f), labelBg);
            canvas.drawText(label, box.left + dp(5f),
                    Math.max(lh, box.top) - dp(4.5f), labelPaint);
        }

        drawStatus(canvas, found.size());
    }

    private void drawMask(Canvas canvas, Segmenter.Instance ins, int colour,
                          float scale, float padX, float padY) {
        int w = ins.maskWidth, h = ins.maskHeight;
        // ALPHA_8 rather than ARGB_8888: the mask is coverage, the colour is
        // uniform. A quarter of the pixels to allocate and upload per frame.
        Bitmap bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ALPHA_8);
        byte[] alpha = new byte[w * h];
        for (int p = 0; p < alpha.length; p++) {
            float v = ins.mask[p];
            alpha[p] = v <= 0f ? 0 : (byte) Math.min(MASK_ALPHA, (int) (v * MASK_ALPHA));
        }
        bmp.copyPixelsFromBuffer(java.nio.ByteBuffer.wrap(alpha));

        // The mask is at input/4, so undoing the letterbox needs that factor too.
        float protoScale = (float) inputSize / (float) w;
        matrix.reset();
        matrix.postScale(protoScale, protoScale);
        matrix.postTranslate(-padX, -padY);
        matrix.postScale(scale, scale);

        maskPaint.setColor(colour);
        canvas.drawBitmap(bmp, matrix, maskPaint);
        bmp.recycle();
    }

    /**
     * The count, the frame rate, and whether this is really running on the GPU.
     *
     * The last one is deliberately visible rather than logged. minSdkVersion is
     * 24, which reaches hardware where 30 fps will not happen at any resolution,
     * and an app that quietly runs at 6 fps on the CPU looks broken in a way
     * nobody can diagnose from the outside. Saying "CPU" is the difference
     * between a bug report and a shrug.
     */
    private void drawStatus(Canvas canvas, int count) {
        String text = count + (count == 1 ? " animal" : " animals")
                + "  ·  " + Math.round(fps) + " fps"
                + (gpu ? "" : "  ·  CPU (slow)");
        float pad = dp(8f);
        float tw = labelPaint.measureText(text);
        float y = getHeight() - dp(14f);
        canvas.drawRoundRect(pad, y - dp(14f), pad + tw + dp(16f), y + dp(6f),
                dp(10f), dp(10f), labelBg);
        canvas.drawText(text, pad + dp(8f), y, labelPaint);
    }

    private float dp(float v) {
        return v * getResources().getDisplayMetrics().density;
    }

    /** Unused, but explicit: this view must never intercept touches. The WebView
     *  behind it owns every control the user can press. */
    @Override
    public boolean onTouchEvent(android.view.MotionEvent event) {
        return false;
    }
}
