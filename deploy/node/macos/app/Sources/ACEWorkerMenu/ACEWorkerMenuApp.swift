import SwiftUI

@main
@MainActor
public struct AudioVenturaACEWorkerApp: App {
    @StateObject private var appState = AppState()

    public init() {
        let state = AppState()
        _appState = StateObject(wrappedValue: state)
        state.start()
    }

    public var body: some Scene {
        MenuBarExtra {
            MenuContentView(appState: appState)
        } label: {
            StatusLabelView(supervisor: appState.supervisor)
        }
        .menuBarExtraStyle(.window)

        Window("Model & Runtime Setup", id: "setup") {
            SetupView(appState: appState)
        }
        Window("Settings", id: "settings") {
            SettingsView(appState: appState)
        }
        Window("About AudioVentura ACE Node", id: "about") {
            VStack(spacing: 12) {
                Image(systemName: "waveform")
                    .font(.system(size: 38))
                Text("AudioVentura ACE Node")
                    .font(.title2)
                Text("Native arm64 menu-bar supervisor for the pinned ACE-Step worker.")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
                Text("No prompt, lyrics, audio bytes, or transfer capability is shown here.")
                    .font(.caption)
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
            }
            .padding(30)
            .frame(width: 380)
        }
    }
}
