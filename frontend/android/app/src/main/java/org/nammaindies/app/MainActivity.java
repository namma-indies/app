package org.nammaindies.app;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

import org.nammaindies.app.segment.DogSegmenterPlugin;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Registered before super.onCreate: the bridge builds its plugin
        // registry during that call, so anything added afterwards is invisible
        // to JavaScript and fails as "plugin not implemented".
        registerPlugin(DogSegmenterPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
