// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "ACEWorkerMenu",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "AudioVenturaACEWorker", targets: ["ACEWorkerMenu"]),
    ],
    targets: [
        .executableTarget(
            name: "ACEWorkerMenu",
            path: "Sources/ACEWorkerMenu"
        ),
        .testTarget(
            name: "ACEWorkerMenuTests",
            dependencies: ["ACEWorkerMenu"],
            path: "Tests/ACEWorkerMenuTests"
        ),
    ]
)
