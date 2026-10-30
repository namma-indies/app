package org.nammaindies.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.util.Arrays;
import java.util.Collections;
import java.util.EnumMap;
import java.util.List;
import java.util.Map;
import org.junit.Test;

public class HapticCurvesRendererTest {
    private static final List<HapticCurvesRenderer.Tap> NO_TAPS = Collections.emptyList();
    private static final List<HapticCurvesRenderer.Ramp> NO_RAMPS = Collections.emptyList();

    @Test
    public void aWaveformRampRisesStepByStepAndLastsAsLongAsTheRamp() {
        HapticCurvesRenderer.Waveform wave = HapticCurvesRenderer.waveform(NO_TAPS,
            Collections.singletonList(new HapticCurvesRenderer.Ramp(0, 700, 0.2, 0.8)));
        assertEquals(700, Arrays.stream(wave.timings).sum());
        for (int i = 1; i < wave.amplitudes.length; i++) assertTrue(wave.amplitudes[i] > wave.amplitudes[i - 1]);
        assertTrue(wave.amplitudes[0] < 0.25 * 255);
        assertTrue(wave.amplitudes[wave.amplitudes.length - 1] > 0.75 * 255);
    }

    @Test
    public void aWaveformTapIsAShortPulseAtItsStrengthAfterQuiet() {
        HapticCurvesRenderer.Waveform wave = HapticCurvesRenderer.waveform(
            Collections.singletonList(new HapticCurvesRenderer.Tap(100, 1.0)), NO_RAMPS);
        assertEquals(Arrays.toString(new long[] {100, HapticCurvesRenderer.TAP_MS}), Arrays.toString(wave.timings));
        assertEquals(Arrays.toString(new int[] {0, 255}), Arrays.toString(wave.amplitudes));
    }

    @Test
    public void aTapOnARampIsFeltAboveIt() {
        HapticCurvesRenderer.Waveform wave = HapticCurvesRenderer.waveform(
            Collections.singletonList(new HapticCurvesRenderer.Tap(680, 1.0)),
            Collections.singletonList(new HapticCurvesRenderer.Ramp(0, 700, 0.2, 0.6)));
        assertEquals(255, wave.amplitudes[wave.amplitudes.length - 1]);
        assertEquals(700, Arrays.stream(wave.timings).sum());
    }

    @Test
    public void anythingFeltIsAtLeastTheSmallestAmplitude() {
        assertEquals(0, HapticCurvesRenderer.amplitude(0));
        assertEquals(1, HapticCurvesRenderer.amplitude(0.001));
        assertEquals(255, HapticCurvesRenderer.amplitude(3));
    }

    private static Map<HapticCurvesRenderer.Primitive, Integer> durations(int tick, int click, int slow, int quick) {
        Map<HapticCurvesRenderer.Primitive, Integer> d = new EnumMap<>(HapticCurvesRenderer.Primitive.class);
        d.put(HapticCurvesRenderer.Primitive.TICK, tick);
        d.put(HapticCurvesRenderer.Primitive.CLICK, click);
        d.put(HapticCurvesRenderer.Primitive.SLOW_RISE, slow);
        d.put(HapticCurvesRenderer.Primitive.QUICK_RISE, quick);
        return d;
    }

    @Test
    public void aFlightIsASlowRiseThenAQuickRiseEndingOnTheLandingClick() {
        // A beat: light-up at 188, flight 600..1300, landing at 1300.
        List<HapticCurvesRenderer.Step> steps = HapticCurvesRenderer.composition(
            Arrays.asList(new HapticCurvesRenderer.Tap(188, 0.5), new HapticCurvesRenderer.Tap(1300, 1.0)),
            Collections.singletonList(new HapticCurvesRenderer.Ramp(600, 700, 0.3, 0.8)),
            durations(10, 12, 300, 150));
        assertEquals(4, steps.size());
        assertEquals(HapticCurvesRenderer.Primitive.CLICK, steps.get(0).primitive);
        assertEquals(188, steps.get(0).delay);
        assertEquals(HapticCurvesRenderer.Primitive.SLOW_RISE, steps.get(1).primitive);
        assertEquals(600 - 188 - 12, steps.get(1).delay);
        assertEquals(HapticCurvesRenderer.Primitive.QUICK_RISE, steps.get(2).primitive);
        assertEquals(1300 - 150 - 900, steps.get(2).delay);
        assertEquals(0.8f, steps.get(2).scale, 1e-6);
        assertEquals(HapticCurvesRenderer.Primitive.CLICK, steps.get(3).primitive);
        assertEquals(0, steps.get(3).delay); // lands as the rise ends
    }

    @Test
    public void aSoftTapIsATickAndAShortRampSkipsTheSlowRise() {
        List<HapticCurvesRenderer.Step> steps = HapticCurvesRenderer.composition(
            Collections.singletonList(new HapticCurvesRenderer.Tap(0, 0.3)),
            Collections.singletonList(new HapticCurvesRenderer.Ramp(100, 200, 0.3, 0.8)),
            durations(10, 12, 300, 150));
        assertEquals(HapticCurvesRenderer.Primitive.TICK, steps.get(0).primitive);
        assertEquals(2, steps.size());
        assertEquals(HapticCurvesRenderer.Primitive.QUICK_RISE, steps.get(1).primitive);
        assertEquals(300 - 150 - 10, steps.get(1).delay);
    }

    @Test
    public void primitivesNeverOverlapAndNoDelayIsNegative() {
        List<HapticCurvesRenderer.Step> steps = HapticCurvesRenderer.composition(
            Arrays.asList(new HapticCurvesRenderer.Tap(0, 1), new HapticCurvesRenderer.Tap(5, 1), new HapticCurvesRenderer.Tap(6, 0.1)),
            NO_RAMPS, durations(10, 40, 300, 150));
        for (HapticCurvesRenderer.Step step : steps) {
            assertTrue(step.delay >= 0);
            assertTrue(step.scale >= 0.05f && step.scale <= 1f);
        }
        assertEquals(0, steps.get(1).delay);
    }
}
