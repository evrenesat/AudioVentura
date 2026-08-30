import Combine
import Foundation

@MainActor
public final class AppState: ObservableObject {
    public let supervisor: WorkerSupervisor
    private let preparationRunner: ModelPreparationRunner

    @Published public private(set) var preparationEvents: [ModelPreparationEvent] = []
    @Published public private(set) var setupErrorCode: String?
    @Published public private(set) var isPreparing = false
    private var preparationTask: Task<Void, Never>?

    public init(
        supervisor: WorkerSupervisor = WorkerSupervisor(),
        preparationRunner: ModelPreparationRunner = ModelPreparationRunner()
    ) {
        self.supervisor = supervisor
        self.preparationRunner = preparationRunner
    }

    public var freeBytes: Int64? { supervisor.paths.freeBytes() }
    public var requiredModelBytes: Int64 { 25_253_680_505 }
    public var minimumSetupBytes: Int64 { 55 * 1024 * 1024 * 1024 }
    public var recommendedSetupBytes: Int64 { 70 * 1024 * 1024 * 1024 }
    public var olderModelVersionCount: Int {
        supervisor.paths.modelRevisionDirectories().filter {
            $0.lastPathComponent != WorkerSupervisor.modelRevision
        }.count
    }

    public func start() {
        supervisor.start()
    }

    public func prepareModel(token: String) {
        guard !token.isEmpty, !isPreparing else { return }
        guard supervisor.menuState == .unconfigured || supervisor.menuState == .stopped else {
            setupErrorCode = "stop_worker_before_model_setup"
            return
        }
        guard (freeBytes ?? 0) >= minimumSetupBytes else {
            setupErrorCode = "insufficient_storage"
            return
        }
        do {
            try supervisor.paths.ensureDirectories()
        } catch {
            setupErrorCode = "model_setup_directory_failed"
            return
        }
        isPreparing = true
        setupErrorCode = nil
        preparationEvents = []
        let pythonURL = supervisor.paths.bundledPythonURL
        let downloadRoot = supervisor.paths.downloadCacheRoot
        let environment = [
            "HF_TOKEN": token,
            "ACE_WORKER_HF_CACHE_ROOT": downloadRoot.path,
            "ACE_WORKER_MODEL_REPO": WorkerSupervisor.modelRepo,
            "ACE_WORKER_MODEL_REVISION": WorkerSupervisor.modelRevision,
            "ACE_WORKER_MODEL_TAG": WorkerSupervisor.modelTag,
            "ACE_WORKER_MODEL_MANIFEST_SHA256": WorkerSupervisor.modelManifestSHA256,
            "ACE_NODE_ACCELERATOR": "mps",
            "ACESTEP_MLX_VAE_CHUNK": "512",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        ]
        let runner = preparationRunner
        let eventSink: @Sendable (ModelPreparationEvent) -> Void = { [weak self] event in
            Task { @MainActor [weak self] in
                self?.preparationEvents.append(event)
            }
        }
        preparationTask?.cancel()
        preparationTask = Task { @MainActor [weak self] in
            guard let self else { return }
            defer {
                isPreparing = false
                preparationTask = nil
                // The SecureField value and the child environment are both
                // short-lived; no credential is written to local state.
            }
            let result: Result<[ModelPreparationEvent], Error> = await Task.detached(
                priority: .userInitiated
            ) {
                do {
                    return .success(
                        try runner.run(
                            pythonURL: pythonURL,
                            environment: environment,
                            onEvent: eventSink
                        )
                    )
                } catch {
                    return .failure(error)
                }
            }.value
            guard !Task.isCancelled else { return }
            switch result {
            case .success(let events):
                preparationEvents = events
                do {
                    try activatePreparedModel()
                    try supervisor.paths.ensureDirectories()
                    try writeSetupReceipt()
                    supervisor.start()
                } catch {
                    setupErrorCode = "model_setup_receipt_failed"
                }
            case .failure(let error):
                if let error = error as? ModelPreparationError {
                    setupErrorCode = error.errorDescription
                } else if let error = error as? WorkerModelError {
                    setupErrorCode = error.errorDescription
                } else {
                    setupErrorCode = "model_preparation_failed"
                }
            }
        }
    }

    public func cancelPreparation() {
        guard isPreparing else { return }
        preparationRunner.cancel()
        preparationTask?.cancel()
        setupErrorCode = "model_preparation_cancelled"
    }

    public func cleanupOldModelVersions() {
        guard !isPreparing else { return }
        guard supervisor.menuState == .unconfigured || supervisor.menuState == .stopped else {
            setupErrorCode = "stop_worker_before_cleanup"
            return
        }
        do {
            for directory in supervisor.paths.modelRevisionDirectories()
            where directory.lastPathComponent != WorkerSupervisor.modelRevision {
                try FileManager.default.removeItem(at: directory)
            }
            setupErrorCode = nil
        } catch {
            setupErrorCode = "old_model_cleanup_failed"
        }
    }

    public func stop() {
        if isPreparing {
            cancelPreparation()
            return
        }
        Task { @MainActor [weak supervisor] in
            await supervisor?.stop()
        }
    }

    public func restart() {
        Task { @MainActor [weak supervisor] in
            await supervisor?.restart()
        }
    }

    public func forceRestart() {
        Task { @MainActor [weak supervisor] in
            await supervisor?.forceRestart()
        }
    }

    private func writeSetupReceipt() throws {
        let receipt: [String: Any] = [
            "schema_version": 1,
            "model_repo": WorkerSupervisor.modelRepo,
            "model_revision": WorkerSupervisor.modelRevision,
            "model_manifest_sha256": WorkerSupervisor.modelManifestSHA256,
        ]
        let data = try JSONSerialization.data(withJSONObject: receipt, options: [.sortedKeys])
        try data.write(to: supervisor.paths.setupReceiptURL, options: .atomic)
        try FileManager.default.setAttributes(
            [.posixPermissions: NSNumber(value: Int16(0o600))],
            ofItemAtPath: supervisor.paths.setupReceiptURL.path
        )
    }

    private func activatePreparedModel() throws {
        let fileManager = FileManager.default
        let source = supervisor.paths.downloadCacheRoot.appendingPathComponent(
            "models--evrenesat--audioventura-ace-step-v0.1.8",
            isDirectory: true
        )
        guard fileManager.fileExists(atPath: source.path) else {
            throw WorkerModelError.invalidResponse
        }

        let target = supervisor.paths.modelCacheRoot(revision: WorkerSupervisor.modelRevision)
        let staging = supervisor.paths.modelsRoot.appendingPathComponent(
            ".pending-\(UUID().uuidString)",
            isDirectory: true
        )
        let backup = supervisor.paths.modelsRoot.appendingPathComponent(
            ".previous-\(UUID().uuidString)",
            isDirectory: true
        )
        try fileManager.createDirectory(
            at: staging,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: NSNumber(value: Int16(0o700))]
        )
        defer { try? fileManager.removeItem(at: staging) }
        try fileManager.createSymbolicLink(
            at: staging.appendingPathComponent(source.lastPathComponent, isDirectory: true),
            withDestinationURL: source
        )

        var movedExisting = false
        if fileManager.fileExists(atPath: target.path) {
            try fileManager.moveItem(at: target, to: backup)
            movedExisting = true
        }
        do {
            try fileManager.moveItem(at: staging, to: target)
        } catch {
            if movedExisting {
                try? fileManager.moveItem(at: backup, to: target)
            }
            throw error
        }
        if movedExisting {
            try? fileManager.removeItem(at: backup)
        }
    }
}
