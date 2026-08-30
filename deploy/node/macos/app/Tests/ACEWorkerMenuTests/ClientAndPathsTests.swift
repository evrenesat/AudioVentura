import Foundation
import XCTest

@testable import ACEWorkerMenu

@MainActor
final class ClientAndPathsTests: XCTestCase {
    func testWorkerClientAuthenticatesAndSendsEmptyDrainBody() async throws {
        let requester = RecordingHTTPRequester(
            responses: [
                HTTPResponse(
                    statusCode: 200,
                    body: Data("{\"accepting\":false,\"running\":true,\"queue_depth\":1}".utf8)),
                HTTPResponse(
                    statusCode: 200,
                    body: Data(
                        "{\"status\":\"ready\",\"phase\":\"draining\",\"error_code\":null,\"queue_depth\":1,\"running\":true,\"running_elapsed_seconds\":2.0,\"max_concurrency\":1,\"accepting\":false,\"accelerator\":\"mps\",\"model\":\"acestep-v15-xl-turbo\",\"lm_model\":\"acestep-5Hz-lm-1.7B\"}"
                            .utf8)),
            ]
        )
        let client = try WorkerClient(
            endpoint: URL(string: "http://100.99.150.44:8210")!,
            token: "node-token",
            supervisorToken: "supervisor-token",
            requester: requester
        )

        let drained = try await client.drain()
        let health = try await client.health()

        XCTAssertEqual(drained.queueDepth, 1)
        XCTAssertEqual(health.phase, .draining)
        XCTAssertEqual(requester.requests.count, 2)
        XCTAssertEqual(
            requester.requests[0].value(forHTTPHeaderField: "Authorization"),
            "Bearer supervisor-token"
        )
        XCTAssertEqual(requester.requests[0].httpMethod, "POST")
        XCTAssertEqual(requester.requests[0].httpBody, Data())
        XCTAssertEqual(requester.requests[1].httpMethod, "GET")
        XCTAssertEqual(
            requester.requests[1].value(forHTTPHeaderField: "Authorization"),
            "Bearer node-token"
        )
    }

    func testPathsAndLogRotationStayUnderPrivateRoot() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(
                "AudioVentura ACE Node é tests-" + UUID().uuidString,
                isDirectory: true
            )
        defer { try? FileManager.default.removeItem(at: root) }
        let paths = AppPaths(rootOverride: root)
        XCTAssertNotNil(paths.freeBytes())
        try paths.ensureDirectories()

        XCTAssertTrue(FileManager.default.fileExists(atPath: paths.modelsRoot.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: paths.downloadCacheRoot.path))
        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: paths.databaseURL.deletingLastPathComponent().path))
        XCTAssertEqual(paths.logURL.deletingLastPathComponent(), paths.logsRoot)

        let rotation = LogRotation(logURL: paths.logURL, maximumBytes: 4, retainedFiles: 2)
        try rotation.append(Data("12345".utf8))
        try rotation.append(Data("67890".utf8))
        XCTAssertTrue(FileManager.default.fileExists(atPath: rotation.rotatedURL(1).path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: paths.logURL.path))
    }
}

@MainActor
private final class RecordingHTTPRequester: HTTPRequesting, @unchecked Sendable {
    var responses: [HTTPResponse]
    var requests: [URLRequest] = []

    init(responses: [HTTPResponse]) {
        self.responses = responses
    }

    func request(_ request: URLRequest) async throws -> HTTPResponse {
        requests.append(request)
        return responses.removeFirst()
    }
}
