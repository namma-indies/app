package org.nammaindies.app;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;

/**
 * Turns the web side's haptic score (#145) into what an Android vibrator can play,
 * with no Android types so it can be tested on its own.
 *
 * A score is taps (a short pulse at an intensity) and ramps (intensity rising or
 * falling over a stretch, like the comet's flight building to its landing). Times
 * are ms from the start; intensities are 0..1.
 *
 * Two renderings, best first (HapticCurvesPlugin picks):
 *   composition  Android 12+ phones that have the primitives: TICK or CLICK for a
 *                tap, SLOW_RISE then QUICK_RISE for a ramp, tuned by the maker.
 *   waveform     phones with amplitude control: the score drawn in 10 ms amplitude
 *                steps, so a ramp is a real slope.
 */
final class HapticCurvesRenderer {
    private HapticCurvesRenderer() {}

    static final class Tap {
        final double at;
        final double intensity;

        Tap(double at, double intensity) {
            this.at = at;
            this.intensity = intensity;
        }
    }

    static final class Ramp {
        final double at;
        final double duration;
        final double from;
        final double to;

        Ramp(double at, double duration, double from, double to) {
            this.at = at;
            this.duration = duration;
            this.from = from;
            this.to = to;
        }
    }

    // ---- waveform -------------------------------------------------------------------

    static final int STEP_MS = 10;
    /** How long a tap buzzes in a waveform: short enough to feel like a tap, long enough for the motor to spin up. */
    static final int TAP_MS = 20;

    static final class Waveform {
        final long[] timings;
        final int[] amplitudes;

        Waveform(long[] timings, int[] amplitudes) {
            this.timings = timings;
            this.amplitudes = amplitudes;
        }
    }

    /** 0..1 to Android's amplitude, 1..255 (0 is off, so anything felt is at least 1). */
    static int amplitude(double intensity) {
        if (intensity <= 0) return 0;
        return (int) Math.max(1, Math.round(Math.min(1, intensity) * 255));
    }

    static Waveform waveform(List<Tap> taps, List<Ramp> ramps) {
        double end = 0;
        for (Tap tap : taps) end = Math.max(end, tap.at + TAP_MS);
        for (Ramp ramp : ramps) end = Math.max(end, ramp.at + ramp.duration);
        int steps = (int) Math.ceil(end / STEP_MS);
        int[] level = new int[steps];
        for (Ramp ramp : ramps) {
            if (ramp.duration <= 0) continue;
            for (int i = (int) Math.floor(ramp.at / STEP_MS); i < steps && i * STEP_MS < ramp.at + ramp.duration; i++) {
                double t = Math.min(1, Math.max(0, (i * STEP_MS + STEP_MS / 2.0 - ramp.at) / ramp.duration));
                level[i] = Math.max(level[i], amplitude(ramp.from + (ramp.to - ramp.from) * t));
            }
        }
        for (Tap tap : taps) {
            int first = (int) Math.floor(tap.at / STEP_MS);
            for (int i = first; i < steps && i < first + TAP_MS / STEP_MS; i++) {
                level[i] = Math.max(level[i], amplitude(tap.intensity));
            }
        }
        // Runs of the same amplitude become one segment.
        List<Long> timings = new ArrayList<>();
        List<Integer> amplitudes = new ArrayList<>();
        for (int i = 0; i < steps; i++) {
            int last = amplitudes.size() - 1;
            if (last >= 0 && amplitudes.get(last) == level[i]) {
                timings.set(last, timings.get(last) + STEP_MS);
            } else {
                timings.add((long) STEP_MS);
                amplitudes.add(level[i]);
            }
        }
        long[] t = new long[timings.size()];
        int[] a = new int[amplitudes.size()];
        for (int i = 0; i < t.length; i++) {
            t[i] = timings.get(i);
            a[i] = amplitudes.get(i);
        }
        return new Waveform(t, a);
    }

    // ---- composition ----------------------------------------------------------------

    enum Primitive { TICK, CLICK, SLOW_RISE, QUICK_RISE }

    /** Below this a tap is a TICK (the lighter primitive), from it a CLICK. */
    static final double CLICK_FROM = 0.45;

    static final class Step {
        final Primitive primitive;
        final float scale;
        /** ms of quiet before this primitive, after the previous one ends (how compositions are written). */
        final int delay;

        Step(Primitive primitive, float scale, int delay) {
            this.primitive = primitive;
            this.scale = scale;
            this.delay = delay;
        }
    }

    private static final class Planned {
        final double start;
        final Primitive primitive;
        final double scale;

        Planned(double start, Primitive primitive, double scale) {
            this.start = start;
            this.primitive = primitive;
            this.scale = scale;
        }
    }

    /**
     * The score as composition steps, given how long each primitive lasts on this phone.
     * A ramp is a SLOW_RISE from its start (if there is room for it) and a QUICK_RISE
     * that ends where the ramp ends. Primitives play one after another, so one that would
     * start before the previous has finished waits for it.
     */
    static List<Step> composition(List<Tap> taps, List<Ramp> ramps, Map<Primitive, Integer> durations) {
        List<Planned> planned = new ArrayList<>();
        for (Tap tap : taps) {
            planned.add(new Planned(tap.at, tap.intensity < CLICK_FROM ? Primitive.TICK : Primitive.CLICK, tap.intensity));
        }
        for (Ramp ramp : ramps) {
            int slow = duration(durations, Primitive.SLOW_RISE);
            int quick = duration(durations, Primitive.QUICK_RISE);
            double end = ramp.at + ramp.duration;
            if (slow + quick <= ramp.duration) {
                planned.add(new Planned(ramp.at, Primitive.SLOW_RISE, (ramp.from + ramp.to) / 2));
            }
            planned.add(new Planned(Math.max(ramp.at, end - quick), Primitive.QUICK_RISE, ramp.to));
        }
        planned.sort(Comparator.comparingDouble(p -> p.start));
        List<Step> steps = new ArrayList<>();
        double clock = 0;
        for (Planned p : planned) {
            double start = Math.max(p.start, clock);
            steps.add(new Step(p.primitive, (float) Math.min(1, Math.max(0.05, p.scale)), (int) Math.round(start - clock)));
            clock = start + duration(durations, p.primitive);
        }
        return steps;
    }

    private static int duration(Map<Primitive, Integer> durations, Primitive primitive) {
        Integer ms = durations.get(primitive);
        return ms == null ? 0 : Math.max(0, ms);
    }
}
