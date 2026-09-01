import Foundation

enum JSONDuplicateKeyDetector {
    static func containsDuplicateTopLevelKey(_ data: Data) -> Bool {
        let bytes = Array(data)
        var depth = 0
        var stringStart: Int?
        var escaped = false
        var keys = Set<String>()

        for index in bytes.indices {
            let byte = bytes[index]
            if let start = stringStart {
                if escaped {
                    escaped = false
                } else if byte == 0x5C {
                    escaped = true
                } else if byte == 0x22 {
                    stringStart = nil
                    guard depth == 1 else { continue }
                    var next = index + 1
                    while next < bytes.count,
                        bytes[next] == 0x20 || bytes[next] == 0x09
                            || bytes[next] == 0x0A || bytes[next] == 0x0D
                    {
                        next += 1
                    }
                    guard next < bytes.count, bytes[next] == 0x3A else { continue }
                    let encodedKey = Data(bytes[start...index])
                    guard let key = try? JSONDecoder().decode(String.self, from: encodedKey)
                    else { continue }
                    if !keys.insert(key).inserted {
                        return true
                    }
                }
                continue
            }
            switch byte {
            case 0x22:
                stringStart = index
            case 0x7B:
                depth += 1
            case 0x7D:
                depth = max(0, depth - 1)
            default:
                break
            }
        }
        return false
    }
}

struct DynamicCodingKey: CodingKey {
    let stringValue: String
    let intValue: Int?

    init?(stringValue: String) {
        self.stringValue = stringValue
        intValue = nil
    }

    init?(intValue: Int) {
        stringValue = "\(intValue)"
        self.intValue = intValue
    }
}

public enum WorkerStatus: String, Codable, CaseIterable, Sendable {
    case initializing
    case ready
    case failed
    case stopping
}

public enum WorkerPhase: String, Codable, CaseIterable, Sendable {
    case starting
    case validatingRuntime = "validating_runtime"
    case validatingModel = "validating_model"
    case loadingDit = "loading_dit"
    case loadingLm = "loading_lm"
    case unloadingModel = "unloading_model"
    case modelUnloaded = "model_unloaded"
    case ready
    case draining
    case failed
    case stopping
}

public enum MenuState: String, CaseIterable, Sendable {
    case unconfigured
    case downloading
    case verifying
    case starting
    case ready
    case running
    case runningQueued
    case modelUnloaded
    case draining
    case tailscaleOffline
    case failed
    case stopped

    public var label: String {
        switch self {
        case .unconfigured, .tailscaleOffline:
            "ACE !"
        case .downloading:
            "ACE ↓"
        case .verifying, .starting:
            "ACE ..."
        case .modelUnloaded:
            "ACE idle"
        case .ready:
            "ACE ✓"
        case .running:
            "ACE 1"
        case .runningQueued:
            "ACE N"
        case .draining:
            "ACE N"
        case .failed:
            "ACE x"
        case .stopped:
            "ACE -"
        }
    }

    public var summary: String {
        switch self {
        case .unconfigured:
            "Setup required"
        case .downloading:
            "Downloading pinned model"
        case .verifying:
            "Verifying pinned model"
        case .starting:
            "Starting native worker"
        case .ready:
            "Ready, queue empty"
        case .running:
            "Running"
        case .runningQueued:
            "Running with queued work"
        case .modelUnloaded:
            "Model unloaded; loads on next job"
        case .draining:
            "Restart after queue drains"
        case .tailscaleOffline:
            "Remote unavailable"
        case .failed:
            "Failed; recovery required"
        case .stopped:
            "Worker stopped"
        }
    }
}

public struct WorkerHealth: Codable, Equatable, Sendable {
    public let status: WorkerStatus
    public let phase: WorkerPhase
    public let errorCode: String?
    public let queueDepth: Int
    public let running: Bool
    public let runningElapsedSeconds: Double?
    public let maxConcurrency: Int
    public let accepting: Bool
    public let accelerator: String
    public let model: String
    public let lmModel: String

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case status
        case phase
        case errorCode = "error_code"
        case queueDepth = "queue_depth"
        case running
        case runningElapsedSeconds = "running_elapsed_seconds"
        case maxConcurrency = "max_concurrency"
        case accepting
        case accelerator
        case model
        case lmModel = "lm_model"
    }

    public init(
        status: WorkerStatus,
        phase: WorkerPhase,
        errorCode: String? = nil,
        queueDepth: Int,
        running: Bool,
        runningElapsedSeconds: Double?,
        maxConcurrency: Int = 1,
        accepting: Bool,
        accelerator: String,
        model: String = "acestep-v15-xl-turbo",
        lmModel: String = "acestep-5Hz-lm-1.7B"
    ) throws {
        self.status = status
        self.phase = phase
        self.errorCode = errorCode
        self.queueDepth = queueDepth
        self.running = running
        self.runningElapsedSeconds = runningElapsedSeconds
        self.maxConcurrency = maxConcurrency
        self.accepting = accepting
        self.accelerator = accelerator
        self.model = model
        self.lmModel = lmModel
        try validate()
    }

    public init(from decoder: Decoder) throws {
        let all = try decoder.container(keyedBy: DynamicCodingKey.self)
        let expected = Set(CodingKeys.allCases.map(\.stringValue))
        guard Set(all.allKeys.map(\.stringValue)) == expected else {
            throw WorkerModelError.healthContractMismatch
        }
        let container = try decoder.container(keyedBy: CodingKeys.self)
        status = try container.decode(WorkerStatus.self, forKey: .status)
        phase = try container.decode(WorkerPhase.self, forKey: .phase)
        errorCode = try container.decodeIfPresent(String.self, forKey: .errorCode)
        queueDepth = try container.decode(Int.self, forKey: .queueDepth)
        running = try container.decode(Bool.self, forKey: .running)
        runningElapsedSeconds = try container.decodeIfPresent(
            Double.self,
            forKey: .runningElapsedSeconds
        )
        maxConcurrency = try container.decode(Int.self, forKey: .maxConcurrency)
        accepting = try container.decode(Bool.self, forKey: .accepting)
        accelerator = try container.decode(String.self, forKey: .accelerator)
        model = try container.decode(String.self, forKey: .model)
        lmModel = try container.decode(String.self, forKey: .lmModel)
        try validate()
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(status, forKey: .status)
        try container.encode(phase, forKey: .phase)
        if let errorCode {
            try container.encode(errorCode, forKey: .errorCode)
        } else {
            try container.encodeNil(forKey: .errorCode)
        }
        try container.encode(queueDepth, forKey: .queueDepth)
        try container.encode(running, forKey: .running)
        if let runningElapsedSeconds {
            try container.encode(runningElapsedSeconds, forKey: .runningElapsedSeconds)
        } else {
            try container.encodeNil(forKey: .runningElapsedSeconds)
        }
        try container.encode(maxConcurrency, forKey: .maxConcurrency)
        try container.encode(accepting, forKey: .accepting)
        try container.encode(accelerator, forKey: .accelerator)
        try container.encode(model, forKey: .model)
        try container.encode(lmModel, forKey: .lmModel)
    }

    public var totalJobs: Int {
        queueDepth + (running ? 1 : 0)
    }

    public var menuState: MenuState {
        switch phase {
        case .starting, .validatingRuntime, .validatingModel, .loadingDit, .loadingLm,
            .unloadingModel:
            .starting
        case .modelUnloaded:
            .modelUnloaded
        case .ready:
            if running && queueDepth > 0 {
                .runningQueued
            } else if running {
                .running
            } else if accepting {
                .ready
            } else {
                .draining
            }
        case .draining:
            .draining
        case .failed:
            .failed
        case .stopping:
            .stopped
        }
    }

    public var accessibilitySummary: String {
        var value = menuState.summary
        if running, let elapsed = runningElapsedSeconds {
            value += ", elapsed \(Self.formatElapsed(elapsed))"
        }
        if queueDepth > 0 {
            value += ", \(queueDepth) queued"
        }
        if let errorCode {
            value += ", error \(errorCode)"
        }
        return value
    }

    private func validate() throws {
        guard queueDepth >= 0, queueDepth <= 1_000_000 else {
            throw WorkerModelError.healthContractMismatch
        }
        guard maxConcurrency == 1 else {
            throw WorkerModelError.healthContractMismatch
        }
        if let runningElapsedSeconds {
            guard runningElapsedSeconds.isFinite, runningElapsedSeconds >= 0 else {
                throw WorkerModelError.healthContractMismatch
            }
        }
        if let errorCode {
            guard Self.isSafeErrorCode(errorCode) else {
                throw WorkerModelError.healthContractMismatch
            }
        }
        guard accelerator == "mps" || accelerator == "cuda" else {
            throw WorkerModelError.healthContractMismatch
        }
        guard model == "acestep-v15-xl-turbo", lmModel == "acestep-5Hz-lm-1.7B" else {
            throw WorkerModelError.healthContractMismatch
        }
        if status == .initializing {
            guard phase != .ready, phase != .modelUnloaded, phase != .draining,
                phase != .failed, phase != .stopping
            else {
                throw WorkerModelError.healthContractMismatch
            }
        }
        if status == .ready {
            guard phase == .ready || phase == .modelUnloaded || phase == .draining else {
                throw WorkerModelError.healthContractMismatch
            }
        }
        if status == .failed {
            guard phase == .failed else {
                throw WorkerModelError.healthContractMismatch
            }
        }
        if status == .stopping {
            guard phase == .stopping else {
                throw WorkerModelError.healthContractMismatch
            }
        }
    }

    private static func isSafeErrorCode(_ value: String) -> Bool {
        guard !value.isEmpty, value.utf8.count <= 64 else {
            return false
        }
        var previousUnderscore = false
        for byte in value.utf8 {
            if byte == 95 {
                guard !previousUnderscore else { return false }
                previousUnderscore = true
            } else if (48...57).contains(byte) || (97...122).contains(byte) {
                previousUnderscore = false
            } else {
                return false
            }
        }
        return !previousUnderscore
    }

    private static func formatElapsed(_ value: Double) -> String {
        let seconds = Int(value.rounded(.down))
        return String(format: "%02d:%02d", seconds / 60, seconds % 60)
    }
}

public struct DrainResponse: Codable, Equatable, Sendable {
    public let accepting: Bool
    public let running: Bool
    public let queueDepth: Int

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case accepting
        case running
        case queueDepth = "queue_depth"
    }

    public init(from decoder: Decoder) throws {
        let all = try decoder.container(keyedBy: DynamicCodingKey.self)
        let expected = Set(CodingKeys.allCases.map(\.stringValue))
        guard Set(all.allKeys.map(\.stringValue)) == expected else {
            throw WorkerModelError.healthContractMismatch
        }
        let container = try decoder.container(keyedBy: CodingKeys.self)
        accepting = try container.decode(Bool.self, forKey: .accepting)
        running = try container.decode(Bool.self, forKey: .running)
        queueDepth = try container.decode(Int.self, forKey: .queueDepth)
        guard queueDepth >= 0, queueDepth <= 1_000_000 else {
            throw WorkerModelError.healthContractMismatch
        }
    }

    public init(accepting: Bool, running: Bool, queueDepth: Int) throws {
        guard queueDepth >= 0, queueDepth <= 1_000_000 else {
            throw WorkerModelError.healthContractMismatch
        }
        self.accepting = accepting
        self.running = running
        self.queueDepth = queueDepth
    }
}

public enum WorkerModelError: Error, Equatable, LocalizedError, Sendable {
    case healthContractMismatch
    case responseTooLarge
    case invalidResponse
    case invalidProgress

    public var errorDescription: String? {
        switch self {
        case .healthContractMismatch:
            "health_contract_mismatch"
        case .responseTooLarge:
            "response_too_large"
        case .invalidResponse:
            "invalid_response"
        case .invalidProgress:
            "model_progress_invalid"
        }
    }
}
