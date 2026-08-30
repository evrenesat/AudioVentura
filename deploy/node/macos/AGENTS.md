# Native ACE Node macOS module

This directory owns the arm64 Swift menu-bar shell and the source-controlled
runtime, app, DMG, signing, and verification builders. The Swift process
supervises exactly one bundled `python -m ace_node` child and sees only the
bounded authenticated health/drain contract. It must not receive prompts,
lyrics, audio bytes, capability URLs, or model tensors.

Use Swift 6 language mode and the macOS 14 deployment target. Apple system
frameworks are the only Swift dependencies. Keep process identity checks,
Keychain access, fixed-path Tailscale discovery, bounded logs, and login-item
registration injectable for tests.

Builds are arm64-only. Runtime receipts, lockfiles, and manifests are copied
into the app; models, Hugging Face tokens, `.env` files, user paths, and build
caches never enter the bundle. Ad-hoc signing is development-only. Developer
ID signing, notarization, and publication require the owner's credentials and
explicit approval.
