import SwiftUI

public struct SettingsView: View {
    @ObservedObject public var appState: AppState
    @ObservedObject private var supervisor: WorkerSupervisor

    public init(appState: AppState) {
        self.appState = appState
        _supervisor = ObservedObject(wrappedValue: appState.supervisor)
    }

    public var body: some View {
        Form {
            Toggle(
                "Launch at Login",
                isOn: Binding(
                    get: { supervisor.loginAtLogin },
                    set: { supervisor.setLaunchAtLogin($0) }
                )
            )
            Text("The worker starts only after the pinned model and Tailscale checks pass.")
                .font(.caption)
                .foregroundStyle(.secondary)
            if let error = supervisor.lastErrorCode {
                Text("Last safe status: \(error)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .frame(width: 430, height: 180)
        .navigationTitle("Settings")
    }
}
