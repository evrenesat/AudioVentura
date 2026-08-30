import AppKit
import SwiftUI

public struct MenuContentView: View {
    @ObservedObject public var appState: AppState
    @ObservedObject private var supervisor: WorkerSupervisor
    @Environment(\.openWindow) private var openWindow
    @State private var showForceRestartConfirmation = false

    public init(appState: AppState) {
        self.appState = appState
        _supervisor = ObservedObject(wrappedValue: appState.supervisor)
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(supervisor.menuState.summary)
                    .font(.headline)
                Spacer()
                if let health = supervisor.health {
                    Text("\(health.totalJobs)")
                        .monospacedDigit()
                        .accessibilityLabel("\(health.totalJobs) active or queued jobs")
                }
            }
            if let health = supervisor.health {
                Text("Phase: \(health.phase.rawValue)")
                    .foregroundStyle(.secondary)
                Text("MPS · XL Turbo · 1.7B · batch 1")
                    .font(.caption)
                if health.running, let elapsed = health.runningElapsedSeconds {
                    Text("Running \(Self.formatElapsed(elapsed))")
                        .monospacedDigit()
                }
                Text(health.queueDepth == 0 ? "Queue empty" : "\(health.queueDepth) pending")
            }
            Text(
                supervisor.tailscaleAddress.map { "Tailscale · \($0)" }
                    ?? "Tailscale · unavailable"
            )
            .font(.caption)
            .foregroundStyle(.secondary)
            if let warning = supervisor.memoryPressureWarning {
                Label(warning, systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
                    .font(.caption)
            }
            Divider()
            controls
            Divider()
            Button("Model & Runtime Setup") { openWindow(id: "setup") }
            Button("Settings") { openWindow(id: "settings") }
            Button("About AudioVentura ACE Node") { openWindow(id: "about") }
            Button("Open Logs") { NSWorkspace.shared.open(supervisor.paths.logsRoot) }
            Button("Open Data Folder") {
                NSWorkspace.shared.open(supervisor.paths.applicationSupportRoot)
            }
            Button("Quit") {
                Task { @MainActor in
                    if appState.isPreparing {
                        appState.cancelPreparation()
                        return
                    }
                    guard await supervisor.stop() else { return }
                    NSApplication.shared.terminate(nil)
                }
            }
        }
        .padding(14)
        .frame(width: 320)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(supervisor.accessibilityLabel)
        .onAppear { supervisor.setPopoverVisible(true) }
        .onDisappear { supervisor.setPopoverVisible(false) }
        .confirmationDialog(
            "Force restart the ACE Node worker?",
            isPresented: $showForceRestartConfirmation,
            titleVisibility: .visible
        ) {
            Button("Force Restart", role: .destructive) { appState.forceRestart() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Queued and running work becomes worker_restarted and is not resubmitted.")
        }
    }

    @ViewBuilder
    private var controls: some View {
        if appState.isPreparing {
            Button("Cancel preparation") { appState.cancelPreparation() }
        } else {
            switch supervisor.menuState {
            case .ready, .running, .runningQueued:
                Button("Stop Worker") { appState.stop() }
                Button("Restart Worker (drain)") { appState.restart() }
                DisclosureGroup("Advanced") {
                    Button("Force Restart…") { showForceRestartConfirmation = true }
                }
            case .draining:
                Button("Stop Worker") { appState.stop() }
            case .failed:
                if supervisor.process.isRunning {
                    Button("Force Restart…") { showForceRestartConfirmation = true }
                } else {
                    Button("Start Worker") { appState.start() }
                }
            case .stopped, .tailscaleOffline:
                Button("Start Worker") { appState.start() }
            default:
                Button("Stop Worker") { appState.stop() }
                    .disabled(true)
            }
        }
    }

    private static func formatElapsed(_ value: Double) -> String {
        let seconds = Int(value.rounded(.down))
        return String(format: "%02d:%02d", seconds / 60, seconds % 60)
    }
}

public struct StatusLabelView: View {
    @ObservedObject public var supervisor: WorkerSupervisor

    public init(supervisor: WorkerSupervisor) {
        self.supervisor = supervisor
    }

    public var body: some View {
        Text(supervisor.menuLabel)
            .accessibilityLabel(supervisor.accessibilityLabel)
    }

}
