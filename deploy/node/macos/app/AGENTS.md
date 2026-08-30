# ACE Worker Menu app

This Swift package owns the arm64 macOS menu-bar supervisor for the bundled
ACE Node worker. Keep the public worker contract bounded to authenticated
`healthz` and `POST /v1/supervisor/drain` responses; prompt text, lyrics,
audio bytes, capability URLs, model tensors, and Hugging Face credentials do
not cross into this process.

Use the macOS 14 deployment target and Swift 6 language mode. Keep operating
system integrations injectable in tests, including process identity checks,
Keychain access, Tailscale discovery, login-item registration, sleep
assertions, and clock/timing behavior. Process shutdown may target only a
worker whose PID, executable path, start identity, and application revision
match the private receipt.

The app stores tokens in Keychain and mutable state below the user's private
Application Support/Cache/Logs directories. Models remain outside the app
bundle. Ad-hoc signing is for development; Developer ID signing,
notarization, and release publication are owner-controlled operations.
