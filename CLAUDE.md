# Watchtower / Print Hub

Monorepo: FastAPI server plus React dashboard (`server/`), Sunmi thermal-printer app and
Watchtower Mobile (`android/`), Scout agent (`server/scout.py`).

## Writing code
- Ponytail is default. Laziest solution that works: stdlib and platform features before
  dependencies, one line before fifty, skip speculative code.
- No em-dashes. Use commas, periods, or parentheses. Applies to code, comments, docs,
  commit messages, and chat.
- Comments are few and factual, at the rate a working engineer writes them. Comment only
  non-obvious intent. Do not narrate what the code already says.
- Comments and commit messages address no one. No first or second person, no "we", "you",
  "let's", "note that". State facts about the code.
- Concise prose everywhere.

## Commits and releases
- Author every commit and release as PzmuV1517. No Claude attribution, no Claude-Session
  trailer.
- Commit messages: imperative, plain, factual.
- Cut a GitHub release when a new APK ships. Print Hub tag `vX.Y.Z` (asset `SunmiPrintHub-*`);
  Watchtower Mobile tag `mobile-vX.Y.Z` (asset `WatchtowerMobile-*`).

## Deploy notes
- Camera MJPEG streaming needs the reverse proxy to stop buffering: `proxy_request_buffering
  off` on `/agent/camera/push` and `proxy_buffering off` on `/watchtower/camera/stream`
  (nginx). Lives only in the proxy config, not the repo, so re-add it on a rebuild. Camera
  hosts need `ffmpeg`. See `server/README.md` (Reverse proxy).

## Verify
- Server: `DATA_DIR=$(mktemp -d) python3 smoke_test.py` from `server/`.
- Android: `./gradlew :app:assembleRelease` from `android/`.
