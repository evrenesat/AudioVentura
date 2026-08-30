import Combine
import CryptoKit
import Darwin
import Foundation
import ServiceManagement

@MainActor
public protocol WorkerProcessManaging: AnyObject {
    var processIdentifier: Int32 { get }
    var executablePath: String? { get }
    var startIdentity: String? { get }
    var isRunning: Bool { get }
    var terminationHandler: (() -> Void)? { get set }

    func launch(
        executableURL: URL,
        arguments: [String],
        environment: [String: String],
        workingDirectory: URL?
    ) throws
    func terminate()
    func kill()
}

@MainActor
public final class SystemWorkerProcess: WorkerProcessManaging {
    private var process = Process()
    private let logRotation: LogRotation
    private var outputHandle: FileHandle?

    public var terminationHandler: (() -> Void)?

    public init(logURL: URL) {
        logRotation = LogRotation(logURL: logURL)
    }

    public var processIdentifier: Int32 { process.processIdentifier }
    public var executablePath: String? { process.executableURL?.path }
    public private(set) var startIdentity: String?
    public var isRunning: Bool { process.isRunning }

    public func launch(
        executableURL: URL,
        arguments: [String],
        environment: [String: String],
        workingDirectory: URL?
    ) throws {
        try logRotation.rotateIfNeeded()
        if !FileManager.default.fileExists(atPath: logRotation.logURL.path) {
            try logRotation.append(Data())
        }
        process = Process()
        process.executableURL = executableURL
        process.arguments = arguments
        process.environment = environment
        process.currentDirectoryURL = workingDirectory
        outputHandle = try FileHandle(forWritingTo: logRotation.logURL)
        try outputHandle?.seekToEnd()
        process.standardOutput = outputHandle
        process.standardError = outputHandle
        process.terminationHandler = { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.terminationHandler?()
            }
        }
        try process.run()
        startIdentity = "\(process.processIdentifier):\(Date().timeIntervalSince1970)"
    }

    public func terminate() {
        process.terminate()
        try? outputHandle?.close()
        outputHandle = nil
    }

    public func kill() {
        if process.isRunning {
            _ = Darwin.kill(process.processIdentifier, SIGKILL)
        }
        try? outputHandle?.close()
        outputHandle = nil
    }
}

public struct ProcessIdentity: Equatable, Sendable {
    public let executablePath: String
    public let startIdentity: String

    public init(executablePath: String, startIdentity: String) {
        self.executablePath = executablePath
        self.startIdentity = startIdentity
    }
}

public protocol ProcessIdentityProviding: AnyObject {
    func identity(for pid: Int32) -> ProcessIdentity?
}

public final class SystemProcessIdentityProvider: ProcessIdentityProviding, @unchecked Sendable {
    private let runner: CommandRunning

    public init(runner: CommandRunning = SystemCommandRunner()) {
        self.runner = runner
    }

    public func identity(for pid: Int32) -> ProcessIdentity? {
        guard pid > 0 else { return nil }
        let start = try? runner.run(
            executable: URL(fileURLWithPath: "/bin/ps"),
            arguments: ["-p", "\(pid)", "-o", "lstart="],
            environment: [:],
            timeout: 1,
            outputLimit: 4_096
        )
        let path = try? runner.run(
            executable: URL(fileURLWithPath: "/bin/ps"),
            arguments: ["-p", "\(pid)", "-o", "comm="],
            environment: [:],
            timeout: 1,
            outputLimit: 4_096
        )
        guard let startData = start, let pathData = path else { return nil }
        let startValue = String(decoding: startData, as: UTF8.self).trimmingCharacters(
            in: .whitespacesAndNewlines)
        let pathValue = String(decoding: pathData, as: UTF8.self).trimmingCharacters(
            in: .whitespacesAndNewlines)
        guard !startValue.isEmpty, pathValue.hasPrefix("/") else { return nil }
        return ProcessIdentity(executablePath: pathValue, startIdentity: startValue)
    }
}

public protocol LoginItemManaging: AnyObject {
    var isEnabled: Bool { get }
    func setEnabled(_ enabled: Bool) throws
}

@available(macOS 13.0, *)
public final class MainAppLoginItemManager: LoginItemManaging, @unchecked Sendable {
    public init() {}

    public var isEnabled: Bool {
        SMAppService.mainApp.status == .enabled
    }

    public func setEnabled(_ enabled: Bool) throws {
        if enabled {
            try SMAppService.mainApp.register()
        } else {
            try SMAppService.mainApp.unregister()
        }
    }
}

public protocol SleepActivityManaging: AnyObject {
    func acquire()
    func release()
}

public final class SystemSleepActivity: SleepActivityManaging, @unchecked Sendable {
    private var activity: NSObjectProtocol?

    public init() {}

    public func acquire() {
        guard activity == nil else { return }
        activity = ProcessInfo.processInfo.beginActivity(
            options: [.idleSystemSleepDisabled, .automaticTerminationDisabled],
            reason: "AudioVentura ACE Node is processing work"
        )
    }

    public func release() {
        if let activity {
            ProcessInfo.processInfo.endActivity(activity)
            self.activity = nil
        }
    }
}

@MainActor
public final class WorkerSupervisor: ObservableObject {
    public static let modelRepo = "evrenesat/audioventura-ace-step-v0.1.8"
    public static let modelRevision = "88b8c7fa089446b53382c1040037492463430bed"
    public static let modelTag = "av-v0.1.8-bundle-2"
    public static let modelManifestSHA256 =
        "39a8180ef6852e2dfccb9088efa7231ca7de7e4c05c8d65e3ac5a3e7a5bfd0fc"
    public static let aceStepCommit = "dce621408bee8c31b4fcf4811682eb9359e1bc94"

    @Published public private(set) var menuState: MenuState = .unconfigured
    @Published public private(set) var health: WorkerHealth?
    @Published public private(set) var tailscaleAddress: String?
    @Published public private(set) var lastErrorCode: String?
    @Published public private(set) var memoryPressureWarning: String?

    public let paths: AppPaths
    public let process: WorkerProcessManaging
    public let keychain: SecretStore

    private let discovery: TailscaleAddressDiscovering
    private let clientFactory: (URL, String, String) throws -> any WorkerClienting
    private let loginItem: LoginItemManaging
    private let sleepActivity: SleepActivityManaging
    private let identityProvider: ProcessIdentityProviding
    private let modelReadyOverride: Bool?
    private let clock: () -> Date
    private var client: (any WorkerClienting)?
    private var nodeToken: String?
    private var supervisorToken: String?
    private var workerReceipt: WorkerReceipt?
    private var pollTask: Task<Void, Never>?
    private var recoveryTask: Task<Void, Never>?
    private var discoveryTask: Task<Void, Never>?
    private var addressTransitionTask: Task<Void, Never>?
    private var memoryPressureSource: DispatchSourceMemoryPressure?
    private var stopping = false
    private var wantsWorker = false
    private var popoverVisible = false
    private var crashTimes: [Date] = []

    public init(
        paths: AppPaths = AppPaths(),
        keychain: SecretStore = KeychainStore(),
        discovery: TailscaleAddressDiscovering = TailscaleAddressDiscovery(),
        process: WorkerProcessManaging? = nil,
        clientFactory: @escaping (URL, String, String) throws -> any WorkerClienting = {
            endpoint, token, supervisorToken in
            return try WorkerClient(
                endpoint: endpoint,
                token: token,
                supervisorToken: supervisorToken
            )
        },
        loginItem: LoginItemManaging? = nil,
        sleepActivity: SleepActivityManaging = SystemSleepActivity(),
        identityProvider: ProcessIdentityProviding = SystemProcessIdentityProvider(),
        modelReady: Bool? = nil,
        clock: @escaping () -> Date = Date.init
    ) {
        self.paths = paths
        self.keychain = keychain
        self.discovery = discovery
        self.process = process ?? SystemWorkerProcess(logURL: paths.logURL)
        self.clientFactory = clientFactory
        self.loginItem = loginItem ?? MainAppLoginItemManager()
        self.sleepActivity = sleepActivity
        self.identityProvider = identityProvider
        modelReadyOverride = modelReady
        self.clock = clock
        self.process.terminationHandler = { [weak self] in
            Task { @MainActor [weak self] in
                self?.workerTerminated()
            }
        }
        beginMemoryPressureMonitoring()
    }

    deinit {
        memoryPressureSource?.cancel()
    }

    public var menuLabel: String {
        if let health, [.running, .runningQueued, .draining].contains(menuState) {
            return "ACE \(max(1, health.totalJobs))"
        }
        return menuState.label
    }

    public var accessibilityLabel: String {
        if let health {
            return "AudioVentura ACE Node, \(health.accessibilitySummary)"
        }
        return "AudioVentura ACE Node, \(menuState.summary)"
    }

    public var loginAtLogin: Bool { loginItem.isEnabled }

    public func setPopoverVisible(_ visible: Bool) {
        guard popoverVisible != visible else { return }
        popoverVisible = visible
        if wantsWorker {
            beginPolling()
        }
    }

    public func start() {
        guard !wantsWorker else { return }
        guard modelIsReady else {
            menuState = .unconfigured
            return
        }
        guard !process.isRunning else {
            menuState = .failed
            lastErrorCode = "worker_process_already_running"
            return
        }
        stopping = false
        wantsWorker = true
        do {
            try paths.ensureDirectories()
            try validateStartup()
            nodeToken = try secret(account: KeychainAccount.nodeToken)
            supervisorToken = try secret(account: KeychainAccount.supervisorToken)
            guard let discovered = try discoverTailscale() else {
                beginDiscoveryRetry()
                return
            }
            try launchWorker(at: discovered)
            beginPolling()
        } catch {
            wantsWorker = false
            lastErrorCode = "worker_start_failed"
            menuState = .failed
            sleepActivity.release()
        }
    }

    public func stop() async -> Bool {
        await stop(afterDrain: false)
    }

    private func stop(afterDrain: Bool) async -> Bool {
        guard wantsWorker else {
            menuState = .stopped
            sleepActivity.release()
            return true
        }
        stopping = true
        recoveryTask?.cancel()
        recoveryTask = nil
        discoveryTask?.cancel()
        discoveryTask = nil
        addressTransitionTask?.cancel()
        addressTransitionTask = nil
        if !afterDrain, !(await drainIfNeeded()) {
            stopping = false
            menuState = .draining
            lastErrorCode = "worker_drain_timeout"
            return false
        }
        wantsWorker = false
        if !(await terminateOwnedProcess(force: false)) {
            if ownedProcessMatches() {
                wantsWorker = true
                stopping = false
                menuState = .failed
                lastErrorCode = "worker_shutdown_timeout"
                return false
            }
            stopping = false
            menuState = .failed
            lastErrorCode = "worker_identity_mismatch"
            sleepActivity.release()
            return false
        }
        pollTask?.cancel()
        pollTask = nil
        client = nil
        health = nil
        menuState = .stopped
        sleepActivity.release()
        return true
    }

    public func restart() async {
        guard wantsWorker else {
            start()
            return
        }
        menuState = .draining
        guard await drainIfNeeded() else {
            menuState = .draining
            lastErrorCode = "worker_drain_timeout"
            return
        }
        guard await stop(afterDrain: true), !wantsWorker, !process.isRunning else { return }
        start()
    }

    public func forceRestart() async {
        guard wantsWorker || workerReceipt != nil else { return }
        stopping = true
        wantsWorker = false
        client = nil
        discoveryTask?.cancel()
        discoveryTask = nil
        addressTransitionTask?.cancel()
        addressTransitionTask = nil
        guard await terminateOwnedProcess(force: true) else {
            wantsWorker = true
            stopping = false
            menuState = .failed
            lastErrorCode =
                ownedProcessMatches()
                ? "worker_force_shutdown_failed"
                : "worker_identity_mismatch"
            return
        }
        pollTask?.cancel()
        pollTask = nil
        health = nil
        menuState = .stopped
        sleepActivity.release()
        stopping = false
        start()
    }

    public func setLaunchAtLogin(_ enabled: Bool) {
        do {
            try loginItem.setEnabled(enabled)
        } catch {
            lastErrorCode = "login_item_denied"
            objectWillChange.send()
        }
    }

    public func controllerConfiguration() -> String? {
        guard let address = tailscaleAddress, let token = nodeToken else { return nil }
        return "ACE_NODE_BASE_URL=http://\(address):8210\nACE_NODE_TOKEN=\(token)"
    }

    public func modelIsReadyForSetup() -> Bool { modelIsReady }

    private func beginMemoryPressureMonitoring() {
        let source = DispatchSource.makeMemoryPressureSource(
            eventMask: [.warning, .critical],
            queue: .main
        )
        source.setEventHandler { [weak self] in
            guard let self else { return }
            if source.data.contains(.critical) {
                self.memoryPressureWarning = "Critical memory pressure"
            } else if source.data.contains(.warning) {
                self.memoryPressureWarning = "Constrained memory pressure"
            }
        }
        source.resume()
        memoryPressureSource = source
    }

    private var modelIsReady: Bool {
        if let modelReadyOverride { return modelReadyOverride }
        guard let data = try? Data(contentsOf: paths.setupReceiptURL), data.count <= 65_536,
            let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            Set(value.keys) == [
                "schema_version", "model_repo", "model_revision", "model_manifest_sha256",
            ],
            value["schema_version"] as? Int == 1,
            value["model_repo"] as? String == Self.modelRepo,
            value["model_revision"] as? String == Self.modelRevision,
            value["model_manifest_sha256"] as? String == Self.modelManifestSHA256
        else {
            return false
        }
        return FileManager.default.fileExists(
            atPath: paths.modelCacheRoot(revision: Self.modelRevision).path
        )
    }

    private func validateStartup() throws {
        guard Bundle.main.bundleURL.pathExtension == "app" else { return }
        guard
            let manifestURL = Bundle.main.url(
                forResource: "release-manifest",
                withExtension: "json",
                subdirectory: "receipt"
            )
        else {
            throw ReleaseManifestError.invalid
        }
        let manifest = try ReleaseManifest.load(from: manifestURL)
        guard FileManager.default.isExecutableFile(atPath: paths.bundledPythonURL.path),
            FileManager.default.fileExists(atPath: paths.bundledLockURL.path)
        else {
            throw ReleaseManifestError.invalid
        }
        let lockBytes = try Data(contentsOf: paths.bundledLockURL)
        let lockDigest = SHA256.hash(data: lockBytes).map { String(format: "%02x", $0) }.joined()
        var receiptInput = Data("audioventura-ace-node-runtime-receipt-v1\0".utf8)
        receiptInput.append(contentsOf: manifest.applicationCommit.utf8)
        receiptInput.append(0)
        receiptInput.append(lockBytes)
        let runtimeDigest = SHA256.hash(data: receiptInput).map { String(format: "%02x", $0) }
            .joined()
        guard manifest.architecture == "arm64",
            manifest.deployNodeLockSHA256 == lockDigest,
            manifest.runtimeReceipt == "sha256:\(runtimeDigest)",
            ProcessInfo.processInfo.physicalMemory >= 32 * 1024 * 1024 * 1024,
            (paths.freeBytes() ?? 0) >= 30 * 1024 * 1024 * 1024
        else {
            throw WorkerModelError.invalidResponse
        }
    }

    private func secret(account: String) throws -> String {
        if let existing = try keychain.read(account: account), !existing.isEmpty {
            return existing
        }
        let value = try KeychainStore.randomToken()
        try keychain.write(value, account: account)
        return value
    }

    private func discoverTailscale() throws -> TailscaleAddress? {
        do {
            let value = try discovery.discover()
            tailscaleAddress = value.ipv4
            return value
        } catch {
            tailscaleAddress = nil
            menuState = .tailscaleOffline
            lastErrorCode = (error as? TailscaleError)?.errorDescription
            return nil
        }
    }

    private func launchWorker(at address: TailscaleAddress) throws {
        guard let nodeToken, let supervisorToken else { throw KeychainError.notFound }
        guard !process.isRunning else { throw WorkerModelError.invalidResponse }
        let endpoint = URL(string: "http://\(address.ipv4):8210")!
        client = try clientFactory(endpoint, nodeToken, supervisorToken)
        let environment = launchEnvironment(
            address: address.ipv4, nodeToken: nodeToken, supervisorToken: supervisorToken)
        do {
            try process.launch(
                executableURL: paths.bundledPythonURL,
                arguments: ["-m", "ace_node"],
                environment: environment,
                workingDirectory: paths.bundledRuntimeRoot
            )
            let identity = identityProvider.identity(for: process.processIdentifier)
            guard let executablePath = process.executablePath,
                executablePath.hasPrefix("/"),
                let processStartIdentity = identity?.startIdentity ?? process.startIdentity,
                !processStartIdentity.isEmpty
            else {
                throw WorkerModelError.invalidResponse
            }
            let receipt = WorkerReceipt(
                parentPID: ProcessInfo.processInfo.processIdentifier,
                workerPID: process.processIdentifier,
                processStartIdentity: processStartIdentity,
                executablePath: executablePath,
                applicationRevision: applicationRevision
            )
            try writeReceipt(receipt)
            workerReceipt = receipt
        } catch {
            if process.isRunning {
                process.terminate()
            }
            throw error
        }
        menuState = .starting
        lastErrorCode = nil
    }

    private var applicationRevision: String {
        if let manifest = releaseManifest {
            return manifest.applicationCommit
        }
        return String(repeating: "0", count: 40)
    }

    private var runtimeReceipt: String {
        releaseManifest?.runtimeReceipt ?? ""
    }

    private var releaseManifest: ReleaseManifest? {
        guard
            let url = Bundle.main.url(
                forResource: "release-manifest",
                withExtension: "json",
                subdirectory: "receipt"
            )
        else {
            return nil
        }
        return try? ReleaseManifest.load(from: url)
    }

    private func launchEnvironment(address: String, nodeToken: String, supervisorToken: String)
        -> [String: String]
    {
        let modelRoot = paths.modelCacheRoot(revision: Self.modelRevision)
        return [
            "ACE_NODE_ACCELERATOR": "mps",
            "ACE_NODE_LISTEN_HOST": address,
            "ACE_NODE_LISTEN_PORT": "8210",
            "ACE_NODE_TOKEN": nodeToken,
            "ACE_NODE_SUPERVISOR_TOKEN": supervisorToken,
            "ACE_NODE_DATA_ROOT": paths.applicationSupportRoot.path,
            "ACE_WORKER_HF_CACHE_ROOT": modelRoot.path,
            "ACE_WORKER_MODEL_REPO": Self.modelRepo,
            "ACE_WORKER_MODEL_REVISION": Self.modelRevision,
            "ACE_WORKER_MODEL_TAG": Self.modelTag,
            "ACE_WORKER_MODEL_MANIFEST_SHA256": Self.modelManifestSHA256,
            "ACE_NODE_APPLICATION_REVISION": applicationRevision,
            "ACE_NODE_RUNTIME_LOCK_PATH": paths.bundledLockURL.path,
            "ACE_NODE_RUNTIME_RECEIPT": runtimeReceipt,
            "ACE_STEP_COMMIT": Self.aceStepCommit,
            "ACE_STEP_TAG": "v0.1.8",
            "ACE_TRANSFER_ALLOWED_HOST": "player.evren.io",
            "ACESTEP_MLX_VAE_CHUNK": "512",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        ]
    }

    private func beginPolling() {
        pollTask?.cancel()
        pollTask = Task { @MainActor [weak self] in
            while let self, self.wantsWorker, !Task.isCancelled {
                await self.pollOnce()
                let seconds =
                    self.popoverVisible || self.health?.running == true
                        || (self.health?.queueDepth ?? 0) > 0 ? 1 : 3
                try? await Task.sleep(for: .seconds(seconds))
            }
        }
    }

    private func beginDiscoveryRetry() {
        discoveryTask?.cancel()
        discoveryTask = Task { @MainActor [weak self] in
            while let self, self.wantsWorker, self.client == nil, !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3))
                guard self.wantsWorker, self.client == nil, !Task.isCancelled else { return }
                do {
                    guard let address = try self.discoverTailscale() else { continue }
                    try self.launchWorker(at: address)
                    self.beginPolling()
                    return
                } catch {
                    self.lastErrorCode = "worker_start_failed"
                }
            }
        }
    }

    private func pollOnce() async {
        guard let client else { return }
        do {
            let current = try await client.health()
            health = current
            menuState = current.menuState
            updateSleepActivity(for: current)
        } catch let error as TailscaleError {
            lastErrorCode = error.errorDescription
            menuState = .tailscaleOffline
        } catch {
            if process.isRunning {
                menuState = .tailscaleOffline
                lastErrorCode = "worker_unreachable"
                scheduleAddressReconciliation()
            } else {
                menuState = .failed
                lastErrorCode = "worker_exited"
            }
            sleepActivity.release()
        }
    }

    private func updateSleepActivity(for value: WorkerHealth) {
        let busy =
            value.status == .initializing || value.running || value.queueDepth > 0
            || value.phase == .draining
        if busy {
            sleepActivity.acquire()
        } else {
            sleepActivity.release()
        }
    }

    private func drainIfNeeded() async -> Bool {
        guard let client else { return true }
        do {
            let drained = try await client.drain()
            if !drained.running, drained.queueDepth == 0 {
                return true
            }
            for _ in 0..<150 {
                guard process.isRunning else { return true }
                let current = try? await client.health()
                if current?.running == false, current?.queueDepth == 0 { return true }
                try? await Task.sleep(for: .milliseconds(100))
            }
            return false
        } catch {
            lastErrorCode = "drain_failed"
            return false
        }
    }

    private func scheduleAddressReconciliation() {
        guard addressTransitionTask == nil else { return }
        addressTransitionTask = Task { @MainActor [weak self] in
            guard let self else { return }
            defer { self.addressTransitionTask = nil }
            guard let discovered = try? self.discovery.discover(),
                discovered.ipv4 != self.tailscaleAddress,
                self.wantsWorker,
                self.process.isRunning
            else {
                return
            }
            self.menuState = .draining
            guard await self.drainIfNeeded() else {
                self.menuState = .tailscaleOffline
                return
            }
            guard await self.terminateOwnedProcess(force: false) else {
                self.menuState = .tailscaleOffline
                self.lastErrorCode = "worker_shutdown_timeout"
                return
            }
            self.pollTask?.cancel()
            self.pollTask = nil
            self.client = nil
            self.health = nil
            self.tailscaleAddress = discovered.ipv4
            do {
                try self.launchWorker(at: discovered)
                self.beginPolling()
            } catch {
                self.menuState = .failed
                self.lastErrorCode = "worker_start_failed"
            }
        }
    }

    private func terminateOwnedProcess(force: Bool) async -> Bool {
        guard workerReceipt != nil else { return true }
        guard ownedProcessMatches() else { return false }
        process.terminate()
        let timeout = force ? 10.0 : 15.0
        let deadline = Date().addingTimeInterval(timeout)
        while process.isRunning, Date() < deadline {
            try? await Task.sleep(for: .milliseconds(100))
        }
        if force, process.isRunning, ownedProcessMatches() {
            process.kill()
        }
        if !process.isRunning {
            removeReceipt()
            return true
        }
        return false
    }

    private func ownedProcessMatches() -> Bool {
        guard let receipt = workerReceipt,
            receipt.workerPID == process.processIdentifier,
            receipt.applicationRevision == applicationRevision,
            receipt.executablePath == process.executablePath,
            receipt.processStartIdentity
                == (identityProvider.identity(for: process.processIdentifier)?.startIdentity
                    ?? process.startIdentity)
        else {
            return false
        }
        return process.isRunning || process.processIdentifier > 0
    }

    private func workerTerminated() {
        removeReceipt()
        guard wantsWorker, !stopping else { return }
        let now = clock()
        crashTimes = crashTimes.filter { now.timeIntervalSince($0) < 600 }
        if crashTimes.count >= 3 {
            wantsWorker = false
            menuState = .failed
            lastErrorCode = "worker_crash_limit"
            sleepActivity.release()
            return
        }
        let backoffs: [UInt64] = [2, 5, 15]
        let delay = backoffs[min(crashTimes.count, backoffs.count - 1)]
        crashTimes.append(now)
        recoveryTask?.cancel()
        recoveryTask = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard let self, self.wantsWorker, !Task.isCancelled else { return }
            self.wantsWorker = false
            self.start()
        }
    }

    private func writeReceipt(_ receipt: WorkerReceipt) throws {
        let data = try JSONEncoder().encode(receipt)
        try data.write(to: paths.workerReceiptURL, options: .atomic)
        try FileManager.default.setAttributes(
            [.posixPermissions: NSNumber(value: Int16(0o600))],
            ofItemAtPath: paths.workerReceiptURL.path)
    }

    private func removeReceipt() {
        try? FileManager.default.removeItem(at: paths.workerReceiptURL)
        workerReceipt = nil
    }

    private struct WorkerReceipt: Codable, Equatable, Sendable {
        let parentPID: Int32
        let workerPID: Int32
        let processStartIdentity: String
        let executablePath: String
        let applicationRevision: String

        private enum CodingKeys: String, CodingKey {
            case parentPID = "parent_pid"
            case workerPID = "worker_pid"
            case processStartIdentity = "process_start_identity"
            case executablePath = "executable_path"
            case applicationRevision = "application_revision"
        }
    }
}
