package org.nammaindies.app.segment;

import android.content.Context;
import android.content.res.AssetFileDescriptor;
import android.graphics.RectF;
import android.util.Log;

import org.tensorflow.lite.Interpreter;
import org.tensorflow.lite.gpu.CompatibilityList;
import org.tensorflow.lite.gpu.GpuDelegate;

import java.io.FileInputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.MappedByteBuffer;
import java.nio.channels.FileChannel;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * yolo26n-seg on the phone GPU: one frame in, dog instances with masks out.
 *
 * <h3>Why this model, at this size</h3>
 * The server runs yolo26x at 223 MB and ~314 ms per photo. That is fine for a
 * background task and impossible here, where 30 fps leaves 33 ms for capture,
 * inference, mask assembly and drawing put together. yolo26n-seg is the same
 * family two sizes down: 3.13 M parameters, 6.4 MB, and it separates two dogs
 * in one frame at 320 and even 256 pixels (measured on this project's own
 * fixtures: two dogs found at 0.93/0.86 and 0.90/0.80 respectively).
 *
 * <h3>The output layout, which is not obvious</h3>
 * Two tensors, and the shapes are asserted at load rather than trusted:
 * <pre>
 *   (1, 300, 38)       300 detections, NMS ALREADY APPLIED at export time
 *                      [x1, y1, x2, y2, conf, cls, m0..m31]
 *   (1, 32, S/4, S/4)  32 mask prototypes
 * </pre>
 * A detection's mask is its 32 coefficients multiplied through the prototypes,
 * then sigmoid, then cropped to its own box. NMS being baked in matters more on
 * a phone than on a server: it is the part of YOLO post-processing most likely
 * to be subtly wrong, and reimplementing it inside a 33 ms budget is how that
 * goes wrong quietly.
 *
 * <h3>GPU, and what happens when there is no usable one</h3>
 * The GPU delegate is attempted and the failure is <em>reported</em>, not
 * swallowed. minSdkVersion here is 24, which reaches back to hardware where
 * 30 fps is not going to happen at any resolution, and an app that silently
 * runs at 6 fps on CPU is worse than one that says it cannot do this. See
 * {@link #usingGpu()} — the caller is expected to surface it.
 *
 * Not thread-safe. Feed it from a single analysis executor.
 */
public final class Segmenter implements AutoCloseable {

    private static final String TAG = "Segmenter";

    /** COCO classes. Cats too, matching the server's detect_reid.py: a cat
     *  sighting that silently produces nothing is indistinguishable from a
     *  failure. */
    private static final int COCO_CAT = 15;
    private static final int COCO_DOG = 16;

    /** Matches REID_CONF_THRESHOLD on the server. Deliberately low: a false box
     *  costs one junk overlay a person can ignore, a missed box costs the whole
     *  animal silently. */
    private static final float CONF_THRESHOLD = 0.10f;

    /** 4 box + 1 conf + 1 cls + 32 mask coefficients. */
    private static final int DET_STRIDE = 38;
    private static final int N_PROTOS = 32;
    private static final int MASK_OFFSET = 6;

    /** More than this in one frame and the overlay is noise rather than
     *  information. The server can consider all of them later; this is a
     *  live preview whose job is to let someone see which animal is which. */
    private static final int MAX_INSTANCES = 4;

    /** One detected animal: where it is, how sure, and its mask. */
    public static final class Instance {
        /** Box in INPUT-tensor pixels, i.e. letterboxed model space. Mapping
         *  back to screen space is the caller's job because only the caller
         *  knows how it letterboxed. */
        public final RectF box;
        public final float confidence;
        public final int cocoClass;
        /** Mask at prototype resolution (input/4), row-major, values in [0,1]
         *  already cropped to {@link #box} — outside it every value is 0. */
        public final float[] mask;
        public final int maskWidth;
        public final int maskHeight;

        Instance(RectF box, float confidence, int cocoClass,
                 float[] mask, int maskWidth, int maskHeight) {
            this.box = box;
            this.confidence = confidence;
            this.cocoClass = cocoClass;
            this.mask = mask;
            this.maskWidth = maskWidth;
            this.maskHeight = maskHeight;
        }

        public boolean isDog() {
            return cocoClass == COCO_DOG;
        }
    }

    private final Interpreter interpreter;
    private GpuDelegate gpuDelegate;
    private final boolean gpu;
    private final int inputSize;
    private final int protoSize;

    private final int detIndex;
    private final int protoIndex;
    private final int nDetections;

    // Reused across frames. Allocating these per frame is a straightforward way
    // to spend the whole budget in the garbage collector.
    private final float[][][] detOut;
    private final float[][][][] protoOut;

    public Segmenter(Context context, String assetName) throws IOException {
        MappedByteBuffer model = loadAsset(context, assetName);

        Interpreter.Options options = new Interpreter.Options();
        boolean useGpu = false;
        CompatibilityList compat = new CompatibilityList();
        if (compat.isDelegateSupportedOnThisDevice()) {
            try {
                gpuDelegate = new GpuDelegate(compat.getBestOptionsForThisDevice());
                options.addDelegate(gpuDelegate);
                useGpu = true;
            } catch (Throwable t) {
                // Reported, not hidden. A device that quietly fell back to CPU
                // will miss 30 fps by a wide margin and the caller has to be
                // able to say so instead of looking broken.
                Log.w(TAG, "GPU delegate unavailable; falling back to CPU", t);
                gpuDelegate = null;
            }
        } else {
            Log.w(TAG, "GPU delegate not supported on this device");
        }
        if (!useGpu) {
            // XNNPACK is the difference between unusable and merely slow on the
            // CPU path. Threads capped: this shares a phone with a camera
            // pipeline and a WebView, and oversubscribing makes it worse.
            options.setUseXNNPACK(true);
            options.setNumThreads(Math.min(4, Runtime.getRuntime().availableProcessors()));
        }
        this.gpu = useGpu;

        this.interpreter = new Interpreter(model, options);

        // Discover the two outputs by shape rather than by index. Export order
        // is not a promise, and indexing the wrong one produces masks that land
        // on the wrong pixels — which reads as a bad model, not a bad guess.
        int det = -1, proto = -1;
        for (int i = 0; i < interpreter.getOutputTensorCount(); i++) {
            int[] s = interpreter.getOutputTensor(i).shape();
            if (s.length == 3 && s[2] == DET_STRIDE) det = i;
            else if (s.length == 4 && s[1] == N_PROTOS) proto = i;
        }
        if (det < 0 || proto < 0) {
            close();
            throw new IOException("unexpected model outputs: expected a (1,N," + DET_STRIDE
                    + ") head and a (1," + N_PROTOS + ",h,w) prototype tensor");
        }
        this.detIndex = det;
        this.protoIndex = proto;

        int[] inShape = interpreter.getInputTensor(0).shape();   // (1,S,S,3)
        this.inputSize = inShape[1];
        int[] detShape = interpreter.getOutputTensor(det).shape();
        int[] protoShape = interpreter.getOutputTensor(proto).shape();
        this.nDetections = detShape[1];
        this.protoSize = protoShape[2];

        this.detOut = new float[1][nDetections][DET_STRIDE];
        this.protoOut = new float[1][N_PROTOS][protoSize][protoSize];

        Log.i(TAG, "loaded " + assetName + " input=" + inputSize
                + " detections=" + nDetections + " protos=" + protoSize
                + " backend=" + (gpu ? "GPU" : "CPU/XNNPACK"));
    }

    /** True when inference is on the GPU. False means this device will not hold
     *  30 fps and the UI should say so rather than stutter unexplained. */
    public boolean usingGpu() {
        return gpu;
    }

    public int inputSize() {
        return inputSize;
    }

    /**
     * Run one frame.
     *
     * @param input normalised NHWC float image, {@code inputSize^2 * 3} floats,
     *              RGB in [0,1], letterboxed by the caller.
     * @return dog and cat instances, most confident first, at most
     *         {@link #MAX_INSTANCES}.
     */
    public List<Instance> run(ByteBuffer input) {
        input.rewind();
        java.util.Map<Integer, Object> outputs = new java.util.HashMap<>();
        outputs.put(detIndex, detOut);
        outputs.put(protoIndex, protoOut);
        interpreter.runForMultipleInputsOutputs(new Object[]{input}, outputs);

        List<Instance> found = new ArrayList<>(MAX_INSTANCES);
        float[][] dets = detOut[0];

        // Confidence-ordered, then capped. Sorting 300 rows is cheaper than
        // decoding masks for animals nobody will look at: a mask decode is
        // 32*protoSize^2 multiply-adds, a comparison is one.
        List<float[]> keep = new ArrayList<>();
        for (int i = 0; i < nDetections; i++) {
            float[] d = dets[i];
            int cls = (int) d[5];
            if (d[4] < CONF_THRESHOLD) continue;
            if (cls != COCO_DOG && cls != COCO_CAT) continue;
            keep.add(d);
        }
        keep.sort(Comparator.comparingDouble((float[] d) -> -d[4]));

        for (int i = 0; i < Math.min(MAX_INSTANCES, keep.size()); i++) {
            float[] d = keep.get(i);
            RectF box = new RectF(d[0], d[1], d[2], d[3]);
            found.add(new Instance(box, d[4], (int) d[5],
                    decodeMask(d, box), protoSize, protoSize));
        }
        return found;
    }

    /**
     * A detection's 32 coefficients through the 32 prototypes, sigmoid, cropped
     * to its box.
     *
     * Cropping to the box is not cosmetic. The prototype basis is global, so an
     * unconstrained mask has responses wherever a similar texture appears —
     * another dog, or the same dog's reflection. Without the crop two animals
     * standing near each other bleed into one blob, which defeats the entire
     * point of instance segmentation here.
     */
    private float[] decodeMask(float[] det, RectF boxInInputPx) {
        float[][][] protos = protoOut[0];
        float[] mask = new float[protoSize * protoSize];

        // Box in prototype space. Prototypes are at stride 4 relative to the
        // input tensor.
        float scale = (float) protoSize / (float) inputSize;
        int x0 = clamp((int) Math.floor(boxInInputPx.left * scale), 0, protoSize - 1);
        int y0 = clamp((int) Math.floor(boxInInputPx.top * scale), 0, protoSize - 1);
        int x1 = clamp((int) Math.ceil(boxInInputPx.right * scale), 0, protoSize);
        int y1 = clamp((int) Math.ceil(boxInInputPx.bottom * scale), 0, protoSize);

        // Only inside the box. Everything else stays zero, which is both the
        // crop and a large saving: a dog usually occupies a small part of frame.
        for (int y = y0; y < y1; y++) {
            for (int x = x0; x < x1; x++) {
                float acc = 0f;
                for (int c = 0; c < N_PROTOS; c++) {
                    acc += det[MASK_OFFSET + c] * protos[c][y][x];
                }
                // Sigmoid. Values below the midpoint are dropped to zero here
                // rather than at draw time, so the overlay can treat the array
                // as coverage and not re-threshold it.
                float v = 1f / (1f + (float) Math.exp(-acc));
                mask[y * protoSize + x] = v > 0.5f ? v : 0f;
            }
        }
        return mask;
    }

    private static int clamp(int v, int lo, int hi) {
        return v < lo ? lo : (Math.min(v, hi));
    }

    private static MappedByteBuffer loadAsset(Context context, String name) throws IOException {
        try (AssetFileDescriptor fd = context.getAssets().openFd(name);
             FileInputStream in = fd.createInputStream()) {
            FileChannel channel = in.getChannel();
            return channel.map(FileChannel.MapMode.READ_ONLY,
                    fd.getStartOffset(), fd.getDeclaredLength());
        }
    }

    /** A reusable, correctly-ordered input buffer for {@link #run}. */
    public ByteBuffer newInputBuffer() {
        ByteBuffer b = ByteBuffer.allocateDirect(inputSize * inputSize * 3 * 4);
        b.order(ByteOrder.nativeOrder());
        return b;
    }

    @Override
    public void close() {
        if (interpreter != null) interpreter.close();
        if (gpuDelegate != null) {
            gpuDelegate.close();
            gpuDelegate = null;
        }
    }
}
