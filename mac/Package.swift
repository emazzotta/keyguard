// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "keyguard",
    platforms: [.macOS(.v13)],
    targets: [
        .target(
            name: "KeyguardCore",
            path: "Sources/KeyguardCore"
        ),
        .executableTarget(
            name: "keyguard",
            dependencies: ["KeyguardCore"],
            path: "Sources/keyguard",
            linkerSettings: [
                .linkedFramework("Security"),
                .linkedFramework("LocalAuthentication"),
                .linkedFramework("CryptoKit"),
            ]
        ),
    ]
)
