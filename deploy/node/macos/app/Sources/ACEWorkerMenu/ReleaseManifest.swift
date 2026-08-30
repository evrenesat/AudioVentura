import Foundation

public struct ReleaseManifest: Codable, Equatable, Sendable {
    public let schemaVersion: Int
    public let appVersion: String
    public let bundleID: String
    public let applicationCommit: String
    public let deployNodeLockSHA256: String
    public let runtimeReceipt: String
    public let aceStepCommit: String
    public let modelRepo: String
    public let modelRevision: String
    public let modelManifestSHA256: String
    public let pythonVersion: String
    public let minimumMacOS: String
    public let architecture: String
    public let builtAtUTC: String

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case schemaVersion = "schema_version"
        case appVersion = "app_version"
        case bundleID = "bundle_id"
        case applicationCommit = "application_commit"
        case deployNodeLockSHA256 = "deploy_node_lock_sha256"
        case runtimeReceipt = "runtime_receipt"
        case aceStepCommit = "ace_step_commit"
        case modelRepo = "model_repo"
        case modelRevision = "model_revision"
        case modelManifestSHA256 = "model_manifest_sha256"
        case pythonVersion = "python_version"
        case minimumMacOS = "minimum_macos"
        case architecture
        case builtAtUTC = "built_at_utc"
    }

    public init(from decoder: Decoder) throws {
        let all = try decoder.container(keyedBy: DynamicCodingKey.self)
        let expected = Set(CodingKeys.allCases.map(\.stringValue))
        guard Set(all.allKeys.map(\.stringValue)) == expected else {
            throw ReleaseManifestError.invalid
        }
        let container = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try container.decode(Int.self, forKey: .schemaVersion)
        appVersion = try container.decode(String.self, forKey: .appVersion)
        bundleID = try container.decode(String.self, forKey: .bundleID)
        applicationCommit = try container.decode(String.self, forKey: .applicationCommit)
        deployNodeLockSHA256 = try container.decode(String.self, forKey: .deployNodeLockSHA256)
        runtimeReceipt = try container.decode(String.self, forKey: .runtimeReceipt)
        aceStepCommit = try container.decode(String.self, forKey: .aceStepCommit)
        modelRepo = try container.decode(String.self, forKey: .modelRepo)
        modelRevision = try container.decode(String.self, forKey: .modelRevision)
        modelManifestSHA256 = try container.decode(String.self, forKey: .modelManifestSHA256)
        pythonVersion = try container.decode(String.self, forKey: .pythonVersion)
        minimumMacOS = try container.decode(String.self, forKey: .minimumMacOS)
        architecture = try container.decode(String.self, forKey: .architecture)
        builtAtUTC = try container.decode(String.self, forKey: .builtAtUTC)
        try validate()
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(schemaVersion, forKey: .schemaVersion)
        try container.encode(appVersion, forKey: .appVersion)
        try container.encode(bundleID, forKey: .bundleID)
        try container.encode(applicationCommit, forKey: .applicationCommit)
        try container.encode(deployNodeLockSHA256, forKey: .deployNodeLockSHA256)
        try container.encode(runtimeReceipt, forKey: .runtimeReceipt)
        try container.encode(aceStepCommit, forKey: .aceStepCommit)
        try container.encode(modelRepo, forKey: .modelRepo)
        try container.encode(modelRevision, forKey: .modelRevision)
        try container.encode(modelManifestSHA256, forKey: .modelManifestSHA256)
        try container.encode(pythonVersion, forKey: .pythonVersion)
        try container.encode(minimumMacOS, forKey: .minimumMacOS)
        try container.encode(architecture, forKey: .architecture)
        try container.encode(builtAtUTC, forKey: .builtAtUTC)
    }

    public func validate() throws {
        guard schemaVersion == 1,
            !appVersion.isEmpty,
            bundleID == "io.evren.audioventura.ace-node",
            Self.isSHA(applicationCommit, length: 40),
            Self.isSHA(deployNodeLockSHA256, length: 64),
            Self.isRuntimeReceipt(runtimeReceipt),
            aceStepCommit == "dce621408bee8c31b4fcf4811682eb9359e1bc94",
            modelRepo == "evrenesat/audioventura-ace-step-v0.1.8",
            Self.isSHA(modelRevision, length: 40),
            Self.isSHA(modelManifestSHA256, length: 64),
            pythonVersion.hasPrefix("3.12"),
            minimumMacOS == "14.0",
            architecture == "arm64",
            builtAtUTC.hasSuffix("Z")
        else {
            throw ReleaseManifestError.invalid
        }
    }

    public static func load(from url: URL) throws -> ReleaseManifest {
        let data = try Data(contentsOf: url)
        guard data.count <= 1_048_576 else {
            throw ReleaseManifestError.invalid
        }
        guard !JSONDuplicateKeyDetector.containsDuplicateTopLevelKey(data) else {
            throw ReleaseManifestError.invalid
        }
        return try JSONDecoder().decode(Self.self, from: data)
    }

    private static func isSHA(_ value: String, length: Int) -> Bool {
        value.utf8.count == length
            && value.utf8.allSatisfy { byte in
                (48...57).contains(byte) || (97...102).contains(byte)
            }
    }

    private static func isRuntimeReceipt(_ value: String) -> Bool {
        value.hasPrefix("sha256:") && isSHA(String(value.dropFirst(7)), length: 64)
    }
}

public enum ReleaseManifestError: Error, Equatable, LocalizedError, Sendable {
    case invalid

    public var errorDescription: String? { "release_manifest_invalid" }
}
