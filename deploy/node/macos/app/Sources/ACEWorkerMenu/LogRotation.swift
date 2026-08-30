import Foundation

public final class LogRotation {
    public let logURL: URL
    public let maximumBytes: Int64
    public let retainedFiles: Int
    private let fileManager: FileManager

    public init(
        logURL: URL,
        maximumBytes: Int64 = 10 * 1024 * 1024,
        retainedFiles: Int = 5,
        fileManager: FileManager = .default
    ) {
        self.logURL = logURL
        self.maximumBytes = maximumBytes
        self.retainedFiles = max(1, retainedFiles)
        self.fileManager = fileManager
    }

    public func rotateIfNeeded() throws {
        guard fileManager.fileExists(atPath: logURL.path) else {
            return
        }
        let attributes = try fileManager.attributesOfItem(atPath: logURL.path)
        guard let size = attributes[.size] as? NSNumber, size.int64Value >= maximumBytes else {
            return
        }
        for index in stride(from: retainedFiles - 1, through: 1, by: -1) {
            let source = rotatedURL(index)
            let destination = rotatedURL(index + 1)
            if fileManager.fileExists(atPath: destination.path) {
                try fileManager.removeItem(at: destination)
            }
            if fileManager.fileExists(atPath: source.path) {
                try fileManager.moveItem(at: source, to: destination)
            }
        }
        let first = rotatedURL(1)
        if fileManager.fileExists(atPath: first.path) {
            try fileManager.removeItem(at: first)
        }
        try fileManager.moveItem(at: logURL, to: first)
        _ = fileManager.createFile(
            atPath: logURL.path, contents: Data(),
            attributes: [
                .posixPermissions: NSNumber(value: Int16(0o600))
            ])
    }

    public func append(_ data: Data) throws {
        try fileManager.createDirectory(
            at: logURL.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: [
                .posixPermissions: NSNumber(value: Int16(0o700))
            ]
        )
        try rotateIfNeeded()
        if !fileManager.fileExists(atPath: logURL.path) {
            _ = fileManager.createFile(
                atPath: logURL.path, contents: Data(),
                attributes: [
                    .posixPermissions: NSNumber(value: Int16(0o600))
                ])
        }
        let handle = try FileHandle(forWritingTo: logURL)
        defer { try? handle.close() }
        try handle.seekToEnd()
        try handle.write(contentsOf: data)
        try rotateIfNeeded()
    }

    public func rotatedURL(_ index: Int) -> URL {
        logURL.deletingPathExtension()
            .appendingPathExtension("log.\(index)")
    }
}
