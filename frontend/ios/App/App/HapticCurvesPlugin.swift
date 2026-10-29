import Foundation
import Capacitor
import CoreHaptics

/**
 * Smooth haptic curves for the voice-note suggestion animation (#145), with Core Haptics.
 *
 * The web side (haptics.ts) sends a short score: taps (transient events) and ramps
 * (one continuous event whose intensity and sharpness move from one value to another,
 * like the comet's flight building to its landing). Times are in ms from the start;
 * intensity and sharpness are 0...1. `play` answers with an id that `stop` takes, so
 * the rest of a flight can be stopped if its chip disappears.
 *
 * One engine for the app, created on first use. It shuts itself down when idle and is
 * restarted after the system resets it (a call, the app going to the background).
 * Everything runs on one serial queue, never the main thread.
 */
@objc(HapticCurvesPlugin)
public class HapticCurvesPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "HapticCurvesPlugin"
    public let jsName = "HapticCurves"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "capabilities", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "play", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stop", returnType: CAPPluginReturnPromise),
    ]

    private let queue = DispatchQueue(label: "org.nammaindies.app.haptic-curves")
    private var engine: CHHapticEngine?
    private var players: [String: CHHapticAdvancedPatternPlayer] = [:]

    private var supported: Bool { CHHapticEngine.capabilitiesForHardware().supportsHaptics }

    @objc func capabilities(_ call: CAPPluginCall) {
        let curves = supported
        call.resolve(["curves": curves, "engine": curves ? "corehaptics" : "none"])
    }

    @objc func play(_ call: CAPPluginCall) {
        guard supported else {
            call.reject("Core Haptics is not supported on this device")
            return
        }
        let taps = call.getArray("taps", JSObject.self) ?? []
        let ramps = call.getArray("ramps", JSObject.self) ?? []
        queue.async {
            do {
                let pattern = try HapticCurvesPattern.make(taps: taps, ramps: ramps)
                let player = try self.startedEngine().makeAdvancedPlayer(with: pattern)
                let id = UUID().uuidString
                player.completionHandler = { [weak self] _ in
                    self?.queue.async { self?.players[id] = nil }
                }
                self.players[id] = player
                try player.start(atTime: CHHapticTimeImmediate)
                call.resolve(["id": id])
            } catch {
                call.reject("Could not play the haptic curve: \(error.localizedDescription)")
            }
        }
    }

    @objc func stop(_ call: CAPPluginCall) {
        let id = call.getString("id") ?? ""
        queue.async {
            if let player = self.players.removeValue(forKey: id) {
                try? player.stop(atTime: CHHapticTimeImmediate)
            }
            call.resolve()
        }
    }

    /** The app's engine, started. Only called on `queue`. */
    private func startedEngine() throws -> CHHapticEngine {
        if let engine = engine {
            try engine.start()
            return engine
        }
        let engine = try CHHapticEngine()
        engine.playsHapticsOnly = true
        engine.isAutoShutdownEnabled = true
        engine.stoppedHandler = { [weak self] _ in
            self?.queue.async { self?.players.removeAll() }
        }
        engine.resetHandler = { [weak self] in
            self?.queue.async {
                self?.players.removeAll()
                try? self?.engine?.start()
            }
        }
        try engine.start()
        self.engine = engine
        return engine
    }
}

/** Turns the web side's score into a Core Haptics pattern. */
enum HapticCurvesPattern {
    static func make(taps: [JSObject], ramps: [JSObject]) throws -> CHHapticPattern {
        var events: [CHHapticEvent] = []
        var curves: [CHHapticParameterCurve] = []
        for tap in taps {
            events.append(CHHapticEvent(
                eventType: .hapticTransient,
                parameters: [
                    CHHapticEventParameter(parameterID: .hapticIntensity, value: unit(tap["intensity"])),
                    CHHapticEventParameter(parameterID: .hapticSharpness, value: unit(tap["sharpness"])),
                ],
                relativeTime: seconds(tap["at"])
            ))
        }
        for ramp in ramps {
            let at = seconds(ramp["at"])
            let duration = max(0.01, seconds(ramp["duration"]))
            let from = ramp["from"] as? JSObject ?? [:]
            let to = ramp["to"] as? JSObject ?? [:]
            // A continuous event at full intensity and middle sharpness, shaped by two curves:
            // intensity control multiplies the intensity, sharpness control shifts the sharpness.
            events.append(CHHapticEvent(
                eventType: .hapticContinuous,
                parameters: [
                    CHHapticEventParameter(parameterID: .hapticIntensity, value: 1),
                    CHHapticEventParameter(parameterID: .hapticSharpness, value: 0.5),
                ],
                relativeTime: at,
                duration: duration
            ))
            curves.append(CHHapticParameterCurve(
                parameterID: .hapticIntensityControl,
                controlPoints: [
                    CHHapticParameterCurve.ControlPoint(relativeTime: 0, value: unit(from["intensity"])),
                    CHHapticParameterCurve.ControlPoint(relativeTime: duration, value: unit(to["intensity"])),
                ],
                relativeTime: at
            ))
            curves.append(CHHapticParameterCurve(
                parameterID: .hapticSharpnessControl,
                controlPoints: [
                    CHHapticParameterCurve.ControlPoint(relativeTime: 0, value: unit(from["sharpness"]) - 0.5),
                    CHHapticParameterCurve.ControlPoint(relativeTime: duration, value: unit(to["sharpness"]) - 0.5),
                ],
                relativeTime: at
            ))
        }
        return try CHHapticPattern(events: events, parameterCurves: curves)
    }

    private static func number(_ value: JSValue?) -> Double {
        switch value {
        case let double as Double: return double
        case let int as Int: return Double(int)
        case let float as Float: return Double(float)
        case let number as NSNumber: return number.doubleValue
        default: return 0
        }
    }

    private static func seconds(_ value: JSValue?) -> TimeInterval {
        max(0, number(value)) / 1000
    }

    private static func unit(_ value: JSValue?) -> Float {
        Float(min(1, max(0, number(value))))
    }
}
