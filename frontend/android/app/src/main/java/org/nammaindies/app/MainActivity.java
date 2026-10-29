package org.nammaindies.app;

import android.os.Bundle;
import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Plugins that live in the app itself, not in an npm package: before the bridge starts.
        registerPlugin(HapticCurvesPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
