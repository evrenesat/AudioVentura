import Foundation

public enum ModelPreparationStage: String, Codable, Sendable {
    case starting
    case downloading
    case verifying
    case complete
    case failed
}

public struct ModelPreparationEvent: Codable, Equatable, Sendable {
    public let stage: ModelPreparationStage
    public let downloadedBytes: Int64
    public let totalBytes: Int64
    public let completedFiles: Int
    public let totalFiles: Int
    public let safeErrorCode: String?

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case stage
        case downloadedBytes = "downloaded_bytes"
        case totalBytes = "total_bytes"
        case completedFiles = "completed_files"
        case totalFiles = "total_files"
        case safeErrorCode = "safe_error_code"
    }

    public init(from decoder: Decoder) throws {
        let all = try decoder.container(keyedBy: DynamicCodingKey.self)
        let expected = Set(CodingKeys.allCases.map(\.stringValue))
        guard Set(all.allKeys.map(\.stringValue)) == expected else {
            throw WorkerModelError.invalidProgress
        }
        let container = try decoder.container(keyedBy: CodingKeys.self)
        stage = try container.decode(ModelPreparationStage.self, forKey: .stage)
        downloadedBytes = try container.decode(Int64.self, forKey: .downloadedBytes)
        totalBytes = try container.decode(Int64.self, forKey: .totalBytes)
        completedFiles = try container.decode(Int.self, forKey: .completedFiles)
        totalFiles = try container.decode(Int.self, forKey: .totalFiles)
        safeErrorCode = try container.decodeIfPresent(String.self, forKey: .safeErrorCode)
        guard downloadedBytes >= 0,
            downloadedBytes <= 25_253_680_505,
            totalBytes == 25_253_680_505,
            completedFiles >= 0,
            completedFiles <= 29,
            totalFiles == 29,
            safeErrorCode.map(Self.isSafeErrorCode) ?? true
        else {
            throw WorkerModelError.invalidProgress
        }
        try validateFields()
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(stage, forKey: .stage)
        try container.encode(downloadedBytes, forKey: .downloadedBytes)
        try container.encode(totalBytes, forKey: .totalBytes)
        try container.encode(completedFiles, forKey: .completedFiles)
        try container.encode(totalFiles, forKey: .totalFiles)
        if let safeErrorCode {
            try container.encode(safeErrorCode, forKey: .safeErrorCode)
        } else {
            try container.encodeNil(forKey: .safeErrorCode)
        }
    }

    public init(
        stage: ModelPreparationStage,
        downloadedBytes: Int64,
        completedFiles: Int,
        safeErrorCode: String? = nil
    ) throws {
        self.stage = stage
        self.downloadedBytes = downloadedBytes
        self.totalBytes = 25_253_680_505
        self.completedFiles = completedFiles
        self.totalFiles = 29
        self.safeErrorCode = safeErrorCode
        try validateFields()
    }

    public static func decodeLines(_ data: Data) throws -> [ModelPreparationEvent] {
        guard data.count <= 4 * 1024 * 1024 else {
            throw WorkerModelError.responseTooLarge
        }
        guard let text = String(data: data, encoding: .utf8) else {
            throw WorkerModelError.invalidProgress
        }
        let lines =
            text
            .split(whereSeparator: { $0.isNewline })
        let events = try lines.map { line in
            let data = Data(line.utf8)
            guard !JSONDuplicateKeyDetector.containsDuplicateTopLevelKey(data) else {
                throw WorkerModelError.invalidProgress
            }
            return try JSONDecoder().decode(Self.self, from: data)
        }
        try validateSequence(events)
        return events
    }

    public static func validateSequence(_ events: [Self]) throws {
        guard let first = events.first, first.stage == .starting,
            let last = events.last, last.stage == .complete
        else {
            throw WorkerModelError.invalidProgress
        }
        var previousBytes: Int64 = 0
        var previousFiles = 0
        for event in events {
            guard event.downloadedBytes >= previousBytes, event.completedFiles >= previousFiles
            else {
                throw WorkerModelError.invalidProgress
            }
            previousBytes = event.downloadedBytes
            previousFiles = event.completedFiles
        }
    }

    private func validateFields() throws {
        guard downloadedBytes >= 0,
            downloadedBytes <= 25_253_680_505,
            totalBytes == 25_253_680_505,
            completedFiles >= 0,
            completedFiles <= 29,
            totalFiles == 29,
            safeErrorCode.map(Self.isSafeErrorCode) ?? true
        else {
            throw WorkerModelError.invalidProgress
        }
        if stage == .failed {
            guard safeErrorCode != nil else { throw WorkerModelError.invalidProgress }
        } else {
            guard safeErrorCode == nil else { throw WorkerModelError.invalidProgress }
        }
    }

    private static func isSafeErrorCode(_ value: String) -> Bool {
        guard !value.isEmpty, value.utf8.count <= 64 else { return false }
        return value.utf8.allSatisfy { byte in
            (48...57).contains(byte) || (97...122).contains(byte) || byte == 95
        }
    }

}

public enum ModelPreparationError: Error, Equatable, LocalizedError, Sendable {
    case processFailed
    case outputTooLarge
    case invalidProgress

    public var errorDescription: String? {
        switch self {
        case .processFailed:
            "model_preparation_failed"
        case .outputTooLarge:
            "model_preparation_output_too_large"
        case .invalidProgress:
            "model_progress_invalid"
        }
    }
}

public final class ModelPreparationRunner: @unchecked Sendable {
    public static let arguments = ["-m", "ace_node.model_bundle", "prepare", "--ndjson"]
    public static let outputLimit = 4 * 1024 * 1024
    private let processLock = NSLock()
    private var activeProcess: Process?

    public init() {}

    public func cancel() {
        processLock.lock()
        defer { processLock.unlock() }
        if let activeProcess, activeProcess.isRunning {
            activeProcess.terminate()
        }
    }

    public func run(
        pythonURL: URL,
        environment: [String: String],
        onEvent: (@Sendable (ModelPreparationEvent) -> Void)? = nil
    ) throws -> [ModelPreparationEvent] {
        let process = Process()
        process.executableURL = pythonURL
        process.arguments = Self.arguments
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        processLock.lock()
        activeProcess = process
        processLock.unlock()
        var completed = false
        defer {
            if !completed, process.isRunning {
                process.terminate()
                process.waitUntilExit()
            }
            processLock.lock()
            activeProcess = nil
            processLock.unlock()
        }
        do {
            try process.run()
            var pending = Data()
            var events: [ModelPreparationEvent] = []
            while true {
                let chunk = try pipe.fileHandleForReading.read(upToCount: 65_536) ?? Data()
                if chunk.isEmpty {
                    break
                }
                pending.append(chunk)
                guard pending.count <= Self.outputLimit else {
                    process.terminate()
                    throw ModelPreparationError.outputTooLarge
                }
                while let newline = pending.firstIndex(of: 0x0A) {
                    let line = pending[..<newline]
                    pending.removeSubrange(...newline)
                    guard !line.isEmpty else { continue }
                    guard !JSONDuplicateKeyDetector.containsDuplicateTopLevelKey(line) else {
                        throw ModelPreparationError.invalidProgress
                    }
                    let event = try JSONDecoder().decode(ModelPreparationEvent.self, from: line)
                    events.append(event)
                    onEvent?(event)
                }
            }
            process.waitUntilExit()
            if !pending.isEmpty {
                guard !JSONDuplicateKeyDetector.containsDuplicateTopLevelKey(pending) else {
                    throw ModelPreparationError.invalidProgress
                }
                let event = try JSONDecoder().decode(ModelPreparationEvent.self, from: pending)
                events.append(event)
                onEvent?(event)
            }
            try ModelPreparationEvent.validateSequence(events)
            guard process.terminationStatus == 0 else { throw ModelPreparationError.processFailed }
            completed = true
            return events
        } catch let error as ModelPreparationError {
            throw error
        } catch {
            throw ModelPreparationError.processFailed
        }
    }
}
