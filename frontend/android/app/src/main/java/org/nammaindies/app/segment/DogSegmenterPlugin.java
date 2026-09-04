package org.nammaindies.app.segment;

import android.Manifest;
import android.graphics.Color;
import android.util.Log;
import android.view.ViewGroup;
import android.widget.FrameLayout;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.camera.core.CameraSelector;
import androidx.camera.core.ImageAnalysis;
import androidx.camera.core.ImageProxy;
import androidx.camera.core.Preview;
import androidx.camera.lifecycle.ProcessCameraProvider;
import androidx.camera.view.PreviewView;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import java.nio.ByteBuffer;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Live instance segmentation of the animals in front of the camera.
 *
 * <h3>What this is for</h3>
 * {@code app/detect_reid.best_animal_box} on the server returns exactly one
 * box, the largest, so when two dogs share a frame the second is discarded
 * before it can ever be embedded or matched. This shows a person, while they
 * are still standing there, that there are two animals and which is which —
 * so the capture they take is a deliberate choice rather than a coin flip the
 * server makes for them later.
 *
 * <h3>Why the camera sits behind the WebView</h3>
 * The preview and the mask overlay are native views inserted <em>underneath</em>
 * the WebView, which is made transparent while this runs. That keeps every
 * control in React where the rest of the app's UI lives, while the pixels never
 * cross the bridge. Shipping masks to JavaScript at 30 fps is roughly 6,400
 * floats per animal per frame through a JSON boundary, which is not a budget
 * that exists. Only counts and timings are sent, a few times a second.
 *
 * <h3>What it does not do</h3>
 * It does not capture, upload or embed anything. The existing capture path is
 * untouched; this is a viewfinder that can see. Embedding on-device is a
 * separate question whose answer is no. fp16 preserves the server's
 * similarities to within 0.0004 when measured in torch, but that is not the
 * risk: the Android GPU delegate is non-deterministic across vendors and
 * drivers, so a phone-computed identity vector cannot be compared by cosine
 * against a corpus computed in fp32 on the server. See Segmenter's note.
 */
@CapacitorPlugin(
        name = "DogSegmenter",
        permissions = {
                @Permission(alias = DogSegmenterPlugin.CAMERA, strings = {Manifest.permission.CAMERA})
        }
)
public class DogSegmenterPlugin extends Plugin {

    static final String CAMERA = "camera";
    private static final String TAG = "DogSegmenter";

    /**
     * Default input edge. 320 rather than the model's native 640 because cost
     * scales with pixels: 640 is four times the work for, on this project's own
     * two-dog fixture, no additional animals found (both dogs at 0.93/0.86 at
     * 320 against 0.95/0.92 at 640, plus a spurious 0.19 detection at 640).
     * Callers can override; 256 exists for devices that cannot hold 30 fps.
     */
    private static final int DEFAULT_SIZE = 320;

    /** State goes to JavaScript at this rate, not per frame. The UI needs to
     *  know "two animals, running on GPU"; it does not need 30 updates a second
     *  to say so, and each one is a bridge crossing. */
    private static final long EVENT_INTERVAL_MS = 200;

    private Segmenter segmenter;
    private ExecutorService analysisExecutor;
    private ProcessCameraProvider cameraProvider;
    private FrameLayout container;
    private PreviewView previewView;
    private SegmentOverlay overlay;
    private ByteBuffer inputBuffer;
    private FrameConverter converter;

    private volatile boolean running = false;
    private long lastEventAt = 0;
    private long lastFrameAt = 0;
    private float fps = 0f;

    @PluginMethod
    public void isSupported(PluginCall call) {
        JSObject ret = new JSObject();
        // Whether the model loads and whether it lands on the GPU are different
        // questions, and only the second decides if 30 fps is plausible. Both
        // are reported rather than collapsed into one boolean.
        ret.put("available", true);
        ret.put("gpu", segmenter != null && segmenter.usingGpu());
        ret.put("running", running);
        call.resolve(ret);
    }

    @PluginMethod
    public void start(PluginCall call) {
        if (getPermissionState(CAMERA) != PermissionState.GRANTED) {
            requestPermissionForAlias(CAMERA, call, "onCameraPermission");
            return;
        }
        launch(call);
    }

    @PermissionCallback
    private void onCameraPermission(PluginCall call) {
        if (getPermissionState(CAMERA) != PermissionState.GRANTED) {
            call.reject("camera permission denied");
            return;
        }
        launch(call);
    }

    private void launch(PluginCall call) {
        int requested = call.getInt("size", DEFAULT_SIZE);
        String asset = call.getString("model", "yolo26n_seg_" + requested + "_fp16.tflite");

        getActivity().runOnUiThread(() -> {
            try {
                if (running) {
                    call.resolve(status());
                    return;
                }
                if (segmenter == null) {
                    segmenter = new Segmenter(getContext(), asset);
                    inputBuffer = segmenter.newInputBuffer();
                    converter = new FrameConverter(segmenter.inputSize());
                }
                attachViews();
                bindCamera();
                running = true;
                call.resolve(status());
            } catch (Exception e) {
                Log.e(TAG, "could not start", e);
                teardown();
                call.reject("could not start segmenter: " + e.getMessage(), e);
            }
        });
    }

    @PluginMethod
    public void stop(PluginCall call) {
        getActivity().runOnUiThread(() -> {
            teardown();
            call.resolve();
        });
    }

    /**
     * Inserts the preview and overlay beneath the WebView and makes the WebView
     * transparent.
     *
     * Index 0 is deliberate: the WebView keeps every touch, so React's controls
     * keep working exactly as they do on every other screen. The saved
     * background colour is restored on teardown — leaving the WebView
     * transparent after stopping would make the rest of the app render over
     * whatever happened to be behind it.
     */
    private void attachViews() {
        ViewGroup parent = (ViewGroup) getBridge().getWebView().getParent();
        container = new FrameLayout(getContext());
        previewView = new PreviewView(getContext());
        // PERFORMANCE mode uses a SurfaceView, which composites without a copy.
        // COMPATIBLE would use a TextureView and add one per frame.
        previewView.setImplementationMode(PreviewView.ImplementationMode.PERFORMANCE);
        previewView.setScaleType(PreviewView.ScaleType.FILL_CENTER);
        overlay = new SegmentOverlay(getContext());

        container.addView(previewView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        container.addView(overlay, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        parent.addView(container, 0, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        getBridge().getWebView().setBackgroundColor(Color.TRANSPARENT);
    }

    private void bindCamera() throws Exception {
        AppCompatActivity activity = (AppCompatActivity) getActivity();
        cameraProvider = ProcessCameraProvider.getInstance(getContext()).get();
        analysisExecutor = Executors.newSingleThreadExecutor();

        Preview preview = new Preview.Builder().build();
        preview.setSurfaceProvider(previewView.getSurfaceProvider());

        ImageAnalysis analysis = new ImageAnalysis.Builder()
                // RGBA out of CameraX, so the YUV_420_888 conversion never has
                // to happen here. That conversion is the usual way a real-time
                // pipeline spends its budget before inference starts.
                .setOutputImageFormat(ImageAnalysis.OUTPUT_IMAGE_FORMAT_RGBA_8888)
                // Drop frames rather than queue them. Queueing turns a slow
                // device into a device with growing latency, which feels far
                // worse than a lower frame rate and is harder to notice.
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build();
        analysis.setAnalyzer(analysisExecutor, this::analyze);

        cameraProvider.unbindAll();
        cameraProvider.bindToLifecycle(activity, CameraSelector.DEFAULT_BACK_CAMERA,
                preview, analysis);
    }

    private void analyze(@NonNull ImageProxy image) {
        try {
            if (!running || segmenter == null || converter == null) return;
            converter.convert(image, inputBuffer);
            List<Segmenter.Instance> found = segmenter.run(inputBuffer);

            long now = System.nanoTime();
            if (lastFrameAt != 0) {
                float instant = 1_000_000_000f / (now - lastFrameAt);
                // Smoothed, because a raw per-frame number is unreadable and
                // the question being answered is "is this keeping up".
                fps = fps == 0f ? instant : (fps * 0.9f + instant * 0.1f);
            }
            lastFrameAt = now;

            if (overlay != null) {
                overlay.submit(found, segmenter.inputSize(), segmenter.usingGpu(), fps);
            }
            maybeNotify(found);
        } catch (Throwable t) {
            // One bad frame must not end the preview. A camera that goes black
            // with no explanation is worse than a dropped frame nobody sees.
            Log.w(TAG, "frame failed", t);
        } finally {
            image.close();
        }
    }

    /** Boxes and counts to JavaScript, throttled. Never masks. */
    private void maybeNotify(List<Segmenter.Instance> found) {
        long now = System.currentTimeMillis();
        if (now - lastEventAt < EVENT_INTERVAL_MS) return;
        lastEventAt = now;

        JSArray animals = new JSArray();
        for (Segmenter.Instance ins : found) {
            JSObject o = new JSObject();
            // Normalised to the model's square so the client never has to know
            // the input size, and the numbers stay meaningful if it changes.
            float s = segmenter.inputSize();
            o.put("x", ins.box.left / s);
            o.put("y", ins.box.top / s);
            o.put("w", ins.box.width() / s);
            o.put("h", ins.box.height() / s);
            o.put("confidence", ins.confidence);
            o.put("kind", ins.isDog() ? "dog" : "cat");
            animals.put(o);
        }
        JSObject event = status();
        event.put("animals", animals);
        notifyListeners("animals", event);
    }

    private JSObject status() {
        JSObject o = new JSObject();
        o.put("running", running);
        o.put("gpu", segmenter != null && segmenter.usingGpu());
        o.put("size", segmenter != null ? segmenter.inputSize() : 0);
        o.put("fps", Math.round(fps));
        return o;
    }

    private void teardown() {
        running = false;
        fps = 0f;
        lastFrameAt = 0;
        if (cameraProvider != null) {
            cameraProvider.unbindAll();
            cameraProvider = null;
        }
        if (analysisExecutor != null) {
            analysisExecutor.shutdown();
            analysisExecutor = null;
        }
        if (container != null) {
            ViewGroup parent = (ViewGroup) container.getParent();
            if (parent != null) parent.removeView(container);
            container = null;
            previewView = null;
            overlay = null;
        }
        if (getBridge() != null && getBridge().getWebView() != null) {
            // Restored, or every screen after this one renders over whatever
            // is behind the WebView.
            getBridge().getWebView().setBackgroundColor(Color.WHITE);
        }
    }

    @Override
    protected void handleOnDestroy() {
        teardown();
        if (segmenter != null) {
            segmenter.close();
            segmenter = null;
        }
        if (converter != null) {
            converter.close();
            converter = null;
        }
        super.handleOnDestroy();
    }

    @Override
    protected void handleOnPause() {
        // The camera is a shared, exclusive resource; holding it while
        // backgrounded is how another app fails to open its own.
        if (running) {
            getActivity().runOnUiThread(this::teardown);
        }
        super.handleOnPause();
    }
}
