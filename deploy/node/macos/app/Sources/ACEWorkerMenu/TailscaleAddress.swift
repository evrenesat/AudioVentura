import Foundation

public struct TailscaleAddress: Equatable, Sendable {
    public let executablePath: String
    public let ipv4: String

    public init(executablePath: String, ipv4: String) {
        self.executablePath = executablePath
        self.ipv4 = ipv4
    }
}

public enum TailscaleError: Error, Equatable, LocalizedError, Sendable {
    case missing
    case offline
    case malformed
    case multipleAddresses
    case timedOut
    case outputTooLarge
    case commandFailed

    public var errorDescription: String? {
        switch self {
        case .missing:
            "tailscale_missing"
        case .offline:
            "tailscale_offline"
        case .malformed:
            "tailscale_status_malformed"
        case .multipleAddresses:
            "tailscale_multiple_ipv4_addresses"
        case .timedOut:
            "tailscale_timeout"
        case .outputTooLarge:
            "tailscale_output_too_large"
        case .commandFailed:
            "tailscale_command_failed"
        }
    }
}

public protocol CommandRunning: AnyObject {
    func run(
        executable: URL,
        arguments: [String],
        environment: [String: String],
        timeout: TimeInterval,
        outputLimit: Int
    ) throws -> Data
}

private final class DataBox: @unchecked Sendable {
    var data = Data()
    var error: Error?
}

public final class SystemCommandRunner: CommandRunning, @unchecked Sendable {
    public init() {}

    public func run(
        executable: URL,
        arguments: [String],
        environment: [String: String],
        timeout: TimeInterval,
        outputLimit: Int
    ) throws -> Data {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
        } catch {
            throw TailscaleError.commandFailed
        }

        let box = DataBox()
        let group = DispatchGroup()
        group.enter()
        DispatchQueue.global(qos: .utility).async {
            defer { group.leave() }
            do {
                while true {
                    let remaining = outputLimit + 1 - box.data.count
                    guard remaining > 0 else { break }
                    let chunk =
                        try pipe.fileHandleForReading.read(
                            upToCount: min(65_536, remaining)
                        ) ?? Data()
                    if chunk.isEmpty { break }
                    box.data.append(chunk)
                    if box.data.count > outputLimit { break }
                }
            } catch {
                box.error = error
            }
        }
        let waitResult = group.wait(timeout: .now() + max(0.1, timeout))
        if waitResult == .timedOut {
            if process.isRunning {
                process.terminate()
            }
            _ = group.wait(timeout: .now() + 1)
            throw TailscaleError.timedOut
        }
        if box.data.count > outputLimit {
            if process.isRunning {
                process.terminate()
            }
            process.waitUntilExit()
            throw TailscaleError.outputTooLarge
        }
        if box.error != nil {
            if process.isRunning {
                process.terminate()
            }
            process.waitUntilExit()
            throw TailscaleError.commandFailed
        }
        process.waitUntilExit()
        if process.terminationStatus != 0 {
            throw TailscaleError.commandFailed
        }
        return box.data
    }
}

public protocol TailscaleAddressDiscovering: AnyObject {
    func discover() throws -> TailscaleAddress
}

public final class TailscaleAddressDiscovery: TailscaleAddressDiscovering, @unchecked Sendable {
    public static let fixedExecutablePaths = [
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/usr/local/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
    ]

    private let runner: CommandRunning
    private let executablePaths: [String]
    private let fileExists: (String) -> Bool

    public init(
        runner: CommandRunning = SystemCommandRunner(),
        executablePaths: [String] = TailscaleAddressDiscovery.fixedExecutablePaths,
        fileExists: @escaping (String) -> Bool = {
            FileManager.default.isExecutableFile(atPath: $0)
        }
    ) {
        self.runner = runner
        self.executablePaths = executablePaths
        self.fileExists = fileExists
    }

    public func discover() throws -> TailscaleAddress {
        guard let path = executablePaths.first(where: fileExists) else {
            throw TailscaleError.missing
        }
        let data: Data
        do {
            data = try runner.run(
                executable: URL(fileURLWithPath: path),
                arguments: ["status", "--json"],
                environment: ["TAILSCALE_BE_CLI": "1"],
                timeout: 5,
                outputLimit: 1_048_576
            )
        } catch let error as TailscaleError {
            throw error
        } catch {
            throw TailscaleError.commandFailed
        }
        guard let object = try? JSONSerialization.jsonObject(with: data),
            let top = object as? [String: Any],
            let backendState = top["BackendState"] as? String
        else {
            throw TailscaleError.malformed
        }
        guard backendState == "Running" else {
            throw TailscaleError.offline
        }
        guard let selfObject = top["Self"] as? [String: Any],
            let values = selfObject["TailscaleIPs"] as? [Any],
            values.allSatisfy({ $0 is String })
        else {
            throw TailscaleError.malformed
        }
        let addresses = values.compactMap { $0 as? String }.filter(Self.isValidIPv4)
        guard !addresses.isEmpty else {
            throw TailscaleError.offline
        }
        guard addresses.count == 1 else {
            throw TailscaleError.multipleAddresses
        }
        return TailscaleAddress(executablePath: path, ipv4: addresses[0])
    }

    private static func isValidIPv4(_ value: String) -> Bool {
        let components = value.split(separator: ".", omittingEmptySubsequences: false)
        guard components.count == 4,
            components.allSatisfy({ !$0.isEmpty && Int($0) != nil && (0...255).contains(Int($0)!) })
        else {
            return false
        }
        guard Int(components[0]) == 100, let second = Int(components[1]) else {
            return false
        }
        return (64...127).contains(second)
    }
}
