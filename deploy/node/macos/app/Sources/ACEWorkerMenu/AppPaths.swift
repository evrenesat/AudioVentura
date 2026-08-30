import Foundation

public struct AppPaths {
    public static let productDirectory = "AudioVentura/ACE Node"

    private let fileManager: FileManager
    private let rootOverride: URL?

    public init(fileManager: FileManager = .default, rootOverride: URL? = nil) {
        self.fileManager = fileManager
        self.rootOverride = rootOverride
    }

    public var applicationSupportRoot: URL {
        rootOverride
            ?? directory(.applicationSupportDirectory)
            .appendingPathComponent(Self.productDirectory, isDirectory: true)
    }

    public var cachesRoot: URL {
        rootOverride.map { $0.appendingPathComponent("Caches", isDirectory: true) }
            ?? directory(.cachesDirectory).appendingPathComponent(
                Self.productDirectory, isDirectory: true)
    }

    public var logsRoot: URL {
        rootOverride.map { $0.appendingPathComponent("Logs", isDirectory: true) }
            ?? directory(.libraryDirectory)
            .appendingPathComponent("Logs/AudioVentura/ACE Node", isDirectory: true)
    }

    public var modelsRoot: URL {
        applicationSupportRoot.appendingPathComponent("models", isDirectory: true)
    }

    public var stateRoot: URL {
        applicationSupportRoot.appendingPathComponent("state", isDirectory: true)
    }

    public var downloadCacheRoot: URL {
        cachesRoot.appendingPathComponent("model-download", isDirectory: true)
    }

    public var databaseURL: URL {
        applicationSupportRoot.appendingPathComponent("node.sqlite3")
    }

    public var setupReceiptURL: URL {
        stateRoot.appendingPathComponent("setup.json")
    }

    public var workerReceiptURL: URL {
        stateRoot.appendingPathComponent("worker.json")
    }

    public var logURL: URL {
        logsRoot.appendingPathComponent("ace-node.log")
    }

    public func modelCacheRoot(revision: String) -> URL {
        modelsRoot.appendingPathComponent(revision, isDirectory: true)
    }

    public func modelRevisionDirectories() -> [URL] {
        guard
            let values = try? fileManager.contentsOfDirectory(
                at: modelsRoot,
                includingPropertiesForKeys: [.isDirectoryKey, .isSymbolicLinkKey],
                options: [.skipsHiddenFiles]
            )
        else {
            return []
        }
        return values.filter { url in
            guard
                let resource = try? url.resourceValues(
                    forKeys: [.isDirectoryKey, .isSymbolicLinkKey]
                )
            else {
                return false
            }
            return resource.isDirectory == true && resource.isSymbolicLink != true
        }
    }

    public var bundledRuntimeRoot: URL {
        let resourceRoot = Bundle.main.resourceURL ?? URL(fileURLWithPath: ".")
        return resourceRoot.appendingPathComponent("runtime", isDirectory: true)
    }

    public var bundledPythonURL: URL {
        bundledRuntimeRoot.appendingPathComponent("venv/bin/python")
    }

    public var bundledLockURL: URL {
        bundledRuntimeRoot.appendingPathComponent("receipt/deploy-node-uv.lock")
    }

    public func ensureDirectories() throws {
        for url in [
            applicationSupportRoot, cachesRoot, logsRoot, modelsRoot, stateRoot, downloadCacheRoot,
        ] {
            try fileManager.createDirectory(
                at: url, withIntermediateDirectories: true,
                attributes: [
                    .posixPermissions: NSNumber(value: Int16(0o700))
                ])
            try fileManager.setAttributes(
                [.posixPermissions: NSNumber(value: Int16(0o700))], ofItemAtPath: url.path)
        }
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var modelRoot = modelsRoot
        try modelRoot.setResourceValues(values)
        var downloadRoot = downloadCacheRoot
        try downloadRoot.setResourceValues(values)
    }

    public func freeBytes() -> Int64? {
        var volumeURL = applicationSupportRoot
        while !fileManager.fileExists(atPath: volumeURL.path) {
            let parent = volumeURL.deletingLastPathComponent()
            guard parent != volumeURL else { return nil }
            volumeURL = parent
        }
        let values = try? volumeURL.resourceValues(forKeys: [
            .volumeAvailableCapacityForImportantUsageKey
        ])
        return values?.volumeAvailableCapacityForImportantUsage
    }

    private func directory(_ kind: FileManager.SearchPathDirectory) -> URL {
        fileManager.urls(for: kind, in: .userDomainMask).first ?? URL(fileURLWithPath: ".")
    }
}
