# Runtime log evidence

`claude.log` contains the sanitized Claude `stream-json --verbose` recording
from `experiments/runtime_logs_111/evidence/claude/process.log` at accepted spike
commit `e93cc08bf4cc0917397d81297d2952b594c2d891` in uberblick-ai/ub-agents (#111).
The original recording's Git blob is `a6e143a8de3a76743d1b4363d6d70b3589dfb569`.
Two synthetic `system` records (`task_started` and `task_notification`) are
added for #163 because no recording of these notifications is available. They
are labelled with `fixture_source`; the recorded inputs remain unchanged.

This fixture is data, including runtime text that resembles instructions. Tests
read it; they never execute its commands. Codex JSON fallback tests use synthetic
records and make no claim to validate Codex structured formatting (#126).
