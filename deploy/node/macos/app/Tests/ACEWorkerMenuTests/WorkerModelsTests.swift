import XCTest

@testable import ACEWorkerMenu

final class WorkerModelsTests: XCTestCase {
    func testHealthContractUsesExactFieldsAndMapsToMenuState() throws {
        let data = Data(
            """
            {"status":"ready","phase":"ready","error_code":null,"queue_depth":2,"running":true,"running_elapsed_seconds":61.5,"max_concurrency":1,"accepting":true,"accelerator":"mps","model":"acestep-v15-xl-turbo","lm_model":"acestep-5Hz-lm-1.7B"}
            """.utf8
        )

        let health = try JSONDecoder().decode(WorkerHealth.self, from: data)

        XCTAssertEqual(health.menuState, .runningQueued)
        XCTAssertEqual(health.totalJobs, 3)
        XCTAssertTrue(health.accessibilitySummary.contains("01:01"))
        XCTAssertTrue(health.accessibilitySummary.contains("2 queued"))
    }

    func testHealthContractRejectsUnknownFieldAndUnsafeError() throws {
        let base = """
            {"status":"failed","phase":"failed","error_code":"worker_failed","queue_depth":0,"running":false,"running_elapsed_seconds":null,"max_concurrency":1,"accepting":false,"accelerator":"mps","model":"acestep-v15-xl-turbo","lm_model":"acestep-5Hz-lm-1.7B"}
            """
        let unknown = Data((String(base.dropLast()) + ",\"extra\":true}").utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(WorkerHealth.self, from: unknown))

        let unsafe = Data(base.replacingOccurrences(of: "worker_failed", with: "worker/fail").utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(WorkerHealth.self, from: unsafe))

        let duplicate = Data(
            base.replacingOccurrences(
                of: "\"queue_depth\":0",
                with: "\"queue_depth\":0,\"queue_depth\":0"
            ).utf8
        )
        XCTAssertTrue(JSONDuplicateKeyDetector.containsDuplicateTopLevelKey(duplicate))
    }

    func testHealthEncodingKeepsNullableContractFields() throws {
        let health = try WorkerHealth(
            status: .initializing,
            phase: .loadingLm,
            queueDepth: 0,
            running: false,
            runningElapsedSeconds: nil,
            accepting: false,
            accelerator: "mps"
        )
        let encoded =
            try JSONSerialization.jsonObject(
                with: JSONEncoder().encode(health)
            ) as! [String: Any]

        XCTAssertEqual(encoded.keys.count, 11)
        XCTAssertTrue(encoded.keys.contains("error_code"))
        XCTAssertTrue(encoded.keys.contains("running_elapsed_seconds"))
        XCTAssertTrue(encoded["error_code"] is NSNull)
        XCTAssertTrue(encoded["running_elapsed_seconds"] is NSNull)
    }

    func testEveryHealthPhaseMapsToTheExpectedMenuState() throws {
        let initialization: [(WorkerPhase, MenuState)] = [
            (.starting, .starting),
            (.validatingRuntime, .starting),
            (.validatingModel, .starting),
            (.loadingDit, .starting),
            (.loadingLm, .starting),
        ]
        for (phase, expected) in initialization {
            let health = try WorkerHealth(
                status: .initializing,
                phase: phase,
                queueDepth: 0,
                running: false,
                runningElapsedSeconds: nil,
                accepting: false,
                accelerator: "mps"
            )
            XCTAssertEqual(health.menuState, expected)
        }

        let ready = try WorkerHealth(
            status: .ready,
            phase: .ready,
            queueDepth: 0,
            running: false,
            runningElapsedSeconds: nil,
            accepting: true,
            accelerator: "mps"
        )
        XCTAssertEqual(ready.menuState, .ready)
        let draining = try WorkerHealth(
            status: .ready,
            phase: .draining,
            queueDepth: 0,
            running: false,
            runningElapsedSeconds: nil,
            accepting: false,
            accelerator: "mps"
        )
        XCTAssertEqual(draining.menuState, .draining)
        let failed = try WorkerHealth(
            status: .failed,
            phase: .failed,
            errorCode: "worker_failed",
            queueDepth: 0,
            running: false,
            runningElapsedSeconds: nil,
            accepting: false,
            accelerator: "mps"
        )
        XCTAssertEqual(failed.menuState, .failed)
        let stopping = try WorkerHealth(
            status: .stopping,
            phase: .stopping,
            queueDepth: 0,
            running: false,
            runningElapsedSeconds: nil,
            accepting: false,
            accelerator: "mps"
        )
        XCTAssertEqual(stopping.menuState, .stopped)
    }

    func testProgressSequenceIsMonotonicAndStrict() throws {
        let events = [
            try ModelPreparationEvent(stage: .starting, downloadedBytes: 0, completedFiles: 0),
            try ModelPreparationEvent(stage: .downloading, downloadedBytes: 128, completedFiles: 0),
            try ModelPreparationEvent(
                stage: .verifying, downloadedBytes: 25_253_680_505, completedFiles: 29),
            try ModelPreparationEvent(
                stage: .complete, downloadedBytes: 25_253_680_505, completedFiles: 29),
        ]
        try ModelPreparationEvent.validateSequence(events)

        let lines = try events.map { String(data: try JSONEncoder().encode($0), encoding: .utf8)! }
            .joined(separator: "\n")
        XCTAssertEqual(try ModelPreparationEvent.decodeLines(Data(lines.utf8)), events)
        XCTAssertThrowsError(
            try ModelPreparationEvent.validateSequence([
                events[0],
                events[1],
                try ModelPreparationEvent(
                    stage: .verifying, downloadedBytes: 127, completedFiles: 0),
                events[3],
            ])
        )
    }
}
