import Foundation
import XCTest

@testable import ACEWorkerMenu

final class TailscaleTests: XCTestCase {
    func testDiscoveryUsesFixedExecutableAndMinimalEnvironment() throws {
        for path in TailscaleAddressDiscovery.fixedExecutablePaths {
            let runner = RecordingCommandRunner(
                data: Data(
                    "{\"BackendState\":\"Running\",\"Self\":{\"TailscaleIPs\":[\"100.99.150.44\"]}}"
                        .utf8)
            )
            let discovery = TailscaleAddressDiscovery(
                runner: runner,
                executablePaths: [path],
                fileExists: { $0 == path }
            )

            let address = try discovery.discover()

            XCTAssertEqual(address.ipv4, "100.99.150.44")
            XCTAssertEqual(runner.executable?.path, path)
            XCTAssertEqual(runner.arguments, ["status", "--json"])
            XCTAssertEqual(runner.environment, ["TAILSCALE_BE_CLI": "1"])
            XCTAssertEqual(runner.timeout, 5)
            XCTAssertEqual(runner.outputLimit, 1_048_576)
        }
    }

    func testDiscoveryRejectsOfflineAndAmbiguousAddresses() {
        let offline = RecordingCommandRunner(
            data: Data("{\"BackendState\":\"Stopped\",\"Self\":{\"TailscaleIPs\":[]}}".utf8)
        )
        let offlineDiscovery = TailscaleAddressDiscovery(
            runner: offline,
            executablePaths: ["/fixed/tailscale"],
            fileExists: { _ in true }
        )
        XCTAssertThrowsError(try offlineDiscovery.discover()) { error in
            XCTAssertEqual(error as? TailscaleError, .offline)
        }

        let multiple = RecordingCommandRunner(
            data: Data(
                "{\"BackendState\":\"Running\",\"Self\":{\"TailscaleIPs\":[\"100.99.150.44\",\"100.100.1.2\"]}}"
                    .utf8)
        )
        let multipleDiscovery = TailscaleAddressDiscovery(
            runner: multiple,
            executablePaths: ["/fixed/tailscale"],
            fileExists: { _ in true }
        )
        XCTAssertThrowsError(try multipleDiscovery.discover()) { error in
            XCTAssertEqual(error as? TailscaleError, .multipleAddresses)
        }
    }

    func testDiscoveryRequiresExecutable() {
        let discovery = TailscaleAddressDiscovery(
            runner: RecordingCommandRunner(data: Data()),
            executablePaths: ["/missing/tailscale"],
            fileExists: { _ in false }
        )
        XCTAssertThrowsError(try discovery.discover()) { error in
            XCTAssertEqual(error as? TailscaleError, .missing)
        }
    }

    func testDiscoveryRejectsMalformedStatus() {
        let runner = RecordingCommandRunner(data: Data("{\"Self\":{}}".utf8))
        let discovery = TailscaleAddressDiscovery(
            runner: runner,
            executablePaths: ["/fixed/tailscale"],
            fileExists: { _ in true }
        )
        XCTAssertThrowsError(try discovery.discover()) { error in
            XCTAssertEqual(error as? TailscaleError, .malformed)
        }
    }
}

private final class RecordingCommandRunner: CommandRunning, @unchecked Sendable {
    let data: Data
    var executable: URL?
    var arguments: [String] = []
    var environment: [String: String] = [:]
    var timeout: TimeInterval = 0
    var outputLimit = 0

    init(data: Data) {
        self.data = data
    }

    func run(
        executable: URL,
        arguments: [String],
        environment: [String: String],
        timeout: TimeInterval,
        outputLimit: Int
    ) throws -> Data {
        self.executable = executable
        self.arguments = arguments
        self.environment = environment
        self.timeout = timeout
        self.outputLimit = outputLimit
        return data
    }
}
