# Testing constraints

**Unit tests over end-to-end.** Verify new logic with pytest / Django's test framework rather than standing up the real running system.

- Write focused unit tests for services, tasks, and other business logic as it's added or changed.
- Mocks/patches are fine where they keep a test focused on the unit under test.
- The user runs end-to-end and manual verification themselves — do not dispatch `nightmare-tester` or `logic-tester` against the live system unless explicitly asked to.
- Report what you tested and the results as part of your normal summary; no separate agent-driven E2E pass is required before asking for review.
