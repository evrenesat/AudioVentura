import SwiftUI

public struct SetupView: View {
    @ObservedObject public var appState: AppState
    @ObservedObject private var supervisor: WorkerSupervisor
    @State private var token = ""

    public init(appState: AppState) {
        self.appState = appState
        _supervisor = ObservedObject(wrappedValue: appState.supervisor)
    }

    public var body: some View {
        Form {
            Section("Pinned model") {
                Text("The first setup downloads exactly 25.25 GB across 29 validated files.")
                Text("Free space: \(Self.formatBytes(appState.freeBytes ?? 0))")
                    .foregroundStyle(
                        (appState.freeBytes ?? 0) >= appState.minimumSetupBytes
                            ? Color.secondary
                            : Color.red
                    )
                SecureField("Read-only Hugging Face token", text: $token)
                    .textContentType(.password)
                if appState.isPreparing {
                    Button("Cancel preparation") {
                        appState.cancelPreparation()
                    }
                } else {
                    Button("Download and verify model") {
                        let value = token
                        token = ""
                        appState.prepareModel(token: value)
                    }
                    .disabled(
                        token.isEmpty || (appState.freeBytes ?? 0) < appState.minimumSetupBytes)
                }
                Text("At least 55 GB is required; 70 GB is recommended. The token is not saved.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let event = appState.preparationEvents.last {
                Section("Progress") {
                    Text(event.stage.rawValue.capitalized)
                    ProgressView(
                        value: Double(event.downloadedBytes), total: Double(event.totalBytes))
                    Text(
                        "\(Self.formatBytes(event.downloadedBytes)) of \(Self.formatBytes(event.totalBytes)) · \(event.completedFiles)/\(event.totalFiles) files"
                    )
                    .font(.caption)
                }
            }
            if appState.olderModelVersionCount > 0 {
                Section("Older model versions") {
                    Text("Older validated models are kept until you remove them explicitly.")
                        .font(.caption)
                    Button("Remove older model versions") {
                        appState.cleanupOldModelVersions()
                    }
                }
            }
            if !appState.isPreparing && supervisor.modelIsReadyForSetup() {
                Section("Startup") {
                    Toggle(
                        "Launch at Login",
                        isOn: Binding(
                            get: { supervisor.loginAtLogin },
                            set: { supervisor.setLaunchAtLogin($0) }
                        )
                    )
                }
            }
            if let error = appState.setupErrorCode {
                Text("Setup failed: \(error)")
                    .foregroundStyle(.red)
            }
        }
        .formStyle(.grouped)
        .frame(width: 480, height: 350)
        .navigationTitle("AudioVentura ACE Node Setup")
    }

    private static func formatBytes(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }
}
