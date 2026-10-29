import Capacitor

/**
 * The app's Capacitor screen. Plugins that live in the app itself, rather than in
 * an npm package, are registered here: HapticCurves (#145).
 */
class AppViewController: CAPBridgeViewController {
    override open func capacitorDidLoad() {
        bridge?.registerPluginInstance(HapticCurvesPlugin())
    }
}
