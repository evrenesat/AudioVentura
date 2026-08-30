import Foundation
import XCTest

@testable import ACEWorkerMenu

@MainActor
final class SupervisorTests: XCTestCase {
    func testStartUsesKeychainTokensAndPrivateRuntimeEnvironment() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("ace-supervisor-tests-" + UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let paths = AppPaths(rootOverride: root)
        let keychain = MemorySecretStore()
        let discovery = FixedDiscovery()
        let process = FakeWorkerProcess()
        let client = FakeWorkerClient()
        let sleep = FakeSleepActivity()
        let login = FakeLoginItem()
        let supervisor = WorkerSupervisor(
            paths: paths,
            keychain: keychain,
            discovery: discovery,
            process: process,
            clientFactory: { _, _, _ in client },
            loginItem: login,
            sleepActivity: sleep,
            identityProvider: EmptyIdentityProvider(),
            modelReady: true
        )

        supervisor.start()
        await Task.yield()

        XCTAssertEqual(process.arguments, ["-m", "ace_node"])
        XCTAssertEqual(process.environment["ACE_NODE_ACCELERATOR"], "mps")
        XCTAssertEqual(process.environment["ACE_NODE_LISTEN_HOST"], "100.99.150.44")
        XCTAssertEqual(process.environment["ACE_NODE_LISTEN_PORT"], "8210")
        XCTAssertEqual(process.environment["ACE_NODE_MODEL_REPO"], nil)
        XCTAssertEqual(process.environment["ACESTEP_MLX_VAE_CHUNK"], "512")
        XCTAssertEqual(process.environment["PYTHONNOUSERSITE"], "1")
        XCTAssertEqual(process.environment["PYTHONDONTWRITEBYTECODE"], "1")
        XCTAssertNotNil(process.environment["ACE_NODE_TOKEN"])
        XCTAssertNotNil(process.environment["ACE_NODE_SUPERVISOR_TOKEN"])
        XCTAssertNotEqual(
            process.environment["ACE_NODE_TOKEN"],
            process.environment["ACE_NODE_SUPERVISOR_TOKEN"]
        )
        XCTAssertEqual(supervisor.tailscaleAddress, "100.99.150.44")
        XCTAssertEqual(supervisor.menuState, .ready)
        XCTAssertTrue(keychain.values.keys.contains(KeychainAccount.nodeToken))
        XCTAssertTrue(keychain.values.keys.contains(KeychainAccount.supervisorToken))

        _ = await supervisor.stop()
        XCTAssertEqual(process.terminateCount, 1)
        XCTAssertEqual(process.killCount, 0)
        XCTAssertEqual(supervisor.menuState, .stopped)
        XCTAssertEqual(sleep.acquireCount, 0)
        XCTAssertGreaterThanOrEqual(login.setCount, 0)
    }

    func testSupervisorRefusesToTerminateChangedProcessIdentity() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(
                "ace-supervisor-identity-" + UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let process = FakeWorkerProcess()
        let supervisor = WorkerSupervisor(
            paths: AppPaths(rootOverride: root),
            keychain: MemorySecretStore(),
            discovery: FixedDiscovery(),
            process: process,
            clientFactory: { _, _, _ in FakeWorkerClient() },
            loginItem: FakeLoginItem(),
            sleepActivity: FakeSleepActivity(),
            identityProvider: EmptyIdentityProvider(),
            modelReady: true
        )

        supervisor.start()
        process.startIdentity = "different-process-start"
        _ = await supervisor.stop()

        XCTAssertEqual(process.terminateCount, 0)
        XCTAssertEqual(process.killCount, 0)
        XCTAssertTrue(process.isRunning)
    }

    func testOfflineTailscaleDoesNotLaunchWorker() {
        let process = FakeWorkerProcess()
        let supervisor = WorkerSupervisor(
            paths: AppPaths(
                rootOverride: FileManager.default.temporaryDirectory
                    .appendingPathComponent(
                        "ace-supervisor-offline-" + UUID().uuidString, isDirectory: true)),
            keychain: MemorySecretStore(),
            discovery: OfflineDiscovery(),
            process: process,
            clientFactory: { _, _, _ in FakeWorkerClient() },
            loginItem: FakeLoginItem(),
            sleepActivity: FakeSleepActivity(),
            identityProvider: EmptyIdentityProvider(),
            modelReady: true
        )

        supervisor.start()

        XCTAssertEqual(process.launchCount, 0)
        XCTAssertEqual(supervisor.menuState, .tailscaleOffline)
        XCTAssertEqual(supervisor.lastErrorCode, "tailscale_offline")
    }

    func testSleepActivityFollowsBusyHealth() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("ace-supervisor-sleep-" + UUID().uuidString, isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let sleep = FakeSleepActivity()
        let busyHealth = try WorkerHealth(
            status: .ready,
            phase: .ready,
            queueDepth: 0,
            running: true,
            runningElapsedSeconds: 3,
            accepting: true,
            accelerator: "mps"
        )
        let supervisor = WorkerSupervisor(
            paths: AppPaths(rootOverride: root),
            keychain: MemorySecretStore(),
            discovery: FixedDiscovery(),
            process: FakeWorkerProcess(),
            clientFactory: { _, _, _ in FakeWorkerClient(value: busyHealth) },
            loginItem: FakeLoginItem(),
            sleepActivity: sleep,
            identityProvider: EmptyIdentityProvider(),
            modelReady: true
        )

        supervisor.start()
        await Task.yield()

        XCTAssertEqual(sleep.acquireCount, 1)
        _ = await supervisor.stop()
        XCTAssertGreaterThanOrEqual(sleep.releaseCount, 1)
    }
}

@MainActor
private final class FakeWorkerProcess: WorkerProcessManaging {
    private(set) var processIdentifier: Int32 = 4242
    private(set) var executablePath: String?
    var startIdentity: String? = "worker-start"
    var isRunning = false
    var terminationHandler: (() -> Void)?
    var arguments: [String] = []
    var environment: [String: String] = [:]
    var workingDirectory: URL?
    var launchCount = 0
    var terminateCount = 0
    var killCount = 0

    func launch(
        executableURL: URL,
        arguments: [String],
        environment: [String: String],
        workingDirectory: URL?
    ) throws {
        launchCount += 1
        executablePath = executableURL.path
        self.arguments = arguments
        self.environment = environment
        self.workingDirectory = workingDirectory
        isRunning = true
    }

    func terminate() {
        terminateCount += 1
        isRunning = false
    }

    func kill() {
        killCount += 1
        isRunning = false
    }
}

@MainActor
private final class FakeWorkerClient: WorkerClienting {
    private let value: WorkerHealth

    init(value: WorkerHealth? = nil) {
        self.value =
            value
            ?? (try! WorkerHealth(
                status: .ready,
                phase: .ready,
                queueDepth: 0,
                running: false,
                runningElapsedSeconds: nil,
                accepting: true,
                accelerator: "mps"
            ))
    }

    func health() async throws -> WorkerHealth {
        value
    }

    func drain() async throws -> DrainResponse {
        try DrainResponse(accepting: false, running: false, queueDepth: 0)
    }
}

private final class FixedDiscovery: TailscaleAddressDiscovering {
    func discover() throws -> TailscaleAddress {
        TailscaleAddress(executablePath: "/opt/homebrew/bin/tailscale", ipv4: "100.99.150.44")
    }
}

private final class OfflineDiscovery: TailscaleAddressDiscovering {
    func discover() throws -> TailscaleAddress {
        throw TailscaleError.offline
    }
}

private final class EmptyIdentityProvider: ProcessIdentityProviding {
    func identity(for _: Int32) -> ProcessIdentity? { nil }
}

private final class MemorySecretStore: SecretStore {
    var values: [String: String] = [:]

    func read(account: String) throws -> String? { values[account] }

    func write(_ value: String, account: String) throws {
        values[account] = value
    }

    func delete(account: String) throws {
        values.removeValue(forKey: account)
    }
}

private final class FakeLoginItem: LoginItemManaging {
    var isEnabled = false
    var setCount = 0

    func setEnabled(_ enabled: Bool) throws {
        setCount += 1
        isEnabled = enabled
    }
}

private final class FakeSleepActivity: SleepActivityManaging {
    var acquireCount = 0
    var releaseCount = 0

    func acquire() { acquireCount += 1 }
    func release() { releaseCount += 1 }
}
