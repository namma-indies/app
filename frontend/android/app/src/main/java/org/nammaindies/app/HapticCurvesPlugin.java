package org.nammaindies.app;

import android.content.Context;
import android.os.Build;
import android.os.SystemClock;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.os.VibratorManager;
import androidx.annotation.RequiresApi;
import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import java.util.ArrayList;
import java.util.EnumMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * Smooth haptic curves for the voice-note suggestion animation (#145). The web side
 * (haptics.ts) sends taps and ramps; HapticCurvesRenderer turns them into a
 * VibrationEffect composition (Android 12+ with primitives) or an amplitude waveform
 * (phones with amplitude control). Anything else says it has no curves, and the web
 * side plays its step-by-step ladder through @capacitor/haptics instead.
 *
 * Sharpness is accepted for parity with iOS but has no Android equivalent.
 */
@CapacitorPlugin(name = "HapticCurves")
public class HapticCurvesPlugin extends Plugin {
    private Vibrator vibrator;
    /** The score playing now, and until when, so `stop` never cancels someone else's buzz. */
    private String playing;
    private long playingUntil;

    @Override
    public void load() {
        Context context = getContext();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            VibratorManager manager = (VibratorManager) context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE);
            vibrator = manager == null ? null : manager.getDefaultVibrator();
        } else {
            vibrator = (Vibrator) context.getSystemService(Context.VIBRATOR_SERVICE);
        }
    }

    private String engine() {
        if (vibrator == null || !vibrator.hasVibrator()) return "none";
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S && vibrator.areAllPrimitivesSupported(primitiveIds())) {
            return "composition";
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && vibrator.hasAmplitudeControl()) return "amplitude";
        return "none";
    }

    @PluginMethod
    public void capabilities(PluginCall call) {
        String engine = engine();
        JSObject result = new JSObject();
        result.put("curves", !engine.equals("none"));
        result.put("engine", engine);
        call.resolve(result);
    }

    @PluginMethod
    public void play(PluginCall call) {
        String engine = engine();
        if (engine.equals("none")) {
            call.reject("No haptic curves on this phone");
            return;
        }
        try {
            List<HapticCurvesRenderer.Tap> taps = new ArrayList<>();
            List<HapticCurvesRenderer.Ramp> ramps = new ArrayList<>();
            double end = 0;
            JSArray tapList = call.getArray("taps", new JSArray());
            for (int i = 0; i < tapList.length(); i++) {
                JSONObject tap = tapList.getJSONObject(i);
                taps.add(new HapticCurvesRenderer.Tap(tap.optDouble("at", 0), tap.optDouble("intensity", 0)));
                end = Math.max(end, tap.optDouble("at", 0) + HapticCurvesRenderer.TAP_MS);
            }
            JSArray rampList = call.getArray("ramps", new JSArray());
            for (int i = 0; i < rampList.length(); i++) {
                JSONObject ramp = rampList.getJSONObject(i);
                JSONObject from = ramp.optJSONObject("from");
                JSONObject to = ramp.optJSONObject("to");
                ramps.add(new HapticCurvesRenderer.Ramp(
                    ramp.optDouble("at", 0),
                    ramp.optDouble("duration", 0),
                    from == null ? 0 : from.optDouble("intensity", 0),
                    to == null ? 0 : to.optDouble("intensity", 0)
                ));
                end = Math.max(end, ramp.optDouble("at", 0) + ramp.optDouble("duration", 0));
            }
            if (taps.isEmpty() && ramps.isEmpty()) {
                call.reject("Nothing to play");
                return;
            }
            VibrationEffect effect = engine.equals("composition") ? composed(taps, ramps) : waveform(taps, ramps);
            vibrator.vibrate(effect);
            String id = UUID.randomUUID().toString();
            playing = id;
            playingUntil = SystemClock.uptimeMillis() + (long) Math.ceil(end) + 200;
            JSObject result = new JSObject();
            result.put("id", id);
            call.resolve(result);
        } catch (JSONException | RuntimeException error) {
            call.reject("Could not play the haptic curve: " + error.getMessage());
        }
    }

    @PluginMethod
    public void stop(PluginCall call) {
        String id = call.getString("id");
        if (id != null && id.equals(playing) && SystemClock.uptimeMillis() < playingUntil && vibrator != null) {
            vibrator.cancel();
        }
        if (id != null && id.equals(playing)) playing = null;
        call.resolve();
    }

    @RequiresApi(Build.VERSION_CODES.O)
    private VibrationEffect waveform(List<HapticCurvesRenderer.Tap> taps, List<HapticCurvesRenderer.Ramp> ramps) {
        HapticCurvesRenderer.Waveform wave = HapticCurvesRenderer.waveform(taps, ramps);
        return VibrationEffect.createWaveform(wave.timings, wave.amplitudes, -1);
    }

    @RequiresApi(Build.VERSION_CODES.S)
    private VibrationEffect composed(List<HapticCurvesRenderer.Tap> taps, List<HapticCurvesRenderer.Ramp> ramps) {
        HapticCurvesRenderer.Primitive[] kinds = HapticCurvesRenderer.Primitive.values();
        int[] lengths = vibrator.getPrimitiveDurations(primitiveIds());
        Map<HapticCurvesRenderer.Primitive, Integer> durations = new EnumMap<>(HapticCurvesRenderer.Primitive.class);
        for (int i = 0; i < kinds.length; i++) durations.put(kinds[i], lengths[i]);
        VibrationEffect.Composition composition = VibrationEffect.startComposition();
        for (HapticCurvesRenderer.Step step : HapticCurvesRenderer.composition(taps, ramps, durations)) {
            composition.addPrimitive(primitiveId(step.primitive), step.scale, step.delay);
        }
        return composition.compose();
    }

    /** The primitives, in the order of HapticCurvesRenderer.Primitive. */
    private static int[] primitiveIds() {
        HapticCurvesRenderer.Primitive[] kinds = HapticCurvesRenderer.Primitive.values();
        int[] ids = new int[kinds.length];
        for (int i = 0; i < kinds.length; i++) ids[i] = primitiveId(kinds[i]);
        return ids;
    }

    private static int primitiveId(HapticCurvesRenderer.Primitive primitive) {
        switch (primitive) {
            case TICK: return VibrationEffect.Composition.PRIMITIVE_TICK;
            case CLICK: return VibrationEffect.Composition.PRIMITIVE_CLICK;
            case SLOW_RISE: return VibrationEffect.Composition.PRIMITIVE_SLOW_RISE;
            default: return VibrationEffect.Composition.PRIMITIVE_QUICK_RISE;
        }
    }
}
