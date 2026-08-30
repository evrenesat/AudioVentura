import Foundation
import XCTest

@testable import ACEWorkerMenu

final class ReleaseManifestTests: XCTestCase {
    func testPinnedReleaseManifestDecodesStrictly() throws {
        let value: [String: Any] = [
            "schema_version": 1,
            "app_version": "0.1.0",
            "bundle_id": "io.evren.audioventura.ace-node",
            "application_commit": String(repeating: "a", count: 40),
            "deploy_node_lock_sha256": String(repeating: "b", count: 64),
            "runtime_receipt": "sha256:" + String(repeating: "c", count: 64),
            "ace_step_commit": "dce621408bee8c31b4fcf4811682eb9359e1bc94",
            "model_repo": "evrenesat/audioventura-ace-step-v0.1.8",
            "model_revision": "88b8c7fa089446b53382c1040037492463430bed",
            "model_manifest_sha256": String(repeating: "d", count: 64),
            "python_version": "3.12.9",
            "minimum_macos": "14.0",
            "architecture": "arm64",
            "built_at_utc": "2026-08-31T00:00:00Z",
        ]
        let data = try JSONSerialization.data(withJSONObject: value)

        let manifest = try JSONDecoder().decode(ReleaseManifest.self, from: data)

        XCTAssertEqual(manifest.bundleID, "io.evren.audioventura.ace-node")
        XCTAssertEqual(manifest.architecture, "arm64")
    }

    func testReleaseManifestRejectsUnknownFieldsAndBadReceipt() throws {
        var value: [String: Any] = [
            "schema_version": 1,
            "app_version": "0.1.0",
            "bundle_id": "io.evren.audioventura.ace-node",
            "application_commit": String(repeating: "a", count: 40),
            "deploy_node_lock_sha256": String(repeating: "b", count: 64),
            "runtime_receipt": "sha256:" + String(repeating: "c", count: 64),
            "ace_step_commit": "dce621408bee8c31b4fcf4811682eb9359e1bc94",
            "model_repo": "evrenesat/audioventura-ace-step-v0.1.8",
            "model_revision": "88b8c7fa089446b53382c1040037492463430bed",
            "model_manifest_sha256": String(repeating: "d", count: 64),
            "python_version": "3.12.9",
            "minimum_macos": "14.0",
            "architecture": "arm64",
            "built_at_utc": "2026-08-31T00:00:00Z",
        ]
        value["unexpected"] = true
        XCTAssertThrowsError(
            try JSONDecoder().decode(
                ReleaseManifest.self,
                from: JSONSerialization.data(withJSONObject: value)
            )
        )

        value.removeValue(forKey: "unexpected")
        value["runtime_receipt"] = String(repeating: "c", count: 64)
        XCTAssertThrowsError(
            try JSONDecoder().decode(
                ReleaseManifest.self,
                from: JSONSerialization.data(withJSONObject: value)
            )
        )
    }
}
