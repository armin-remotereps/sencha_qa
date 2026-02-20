# Testing constraints

**E2E testing only — no unit tests, no pytest, no mocking.**

All features must be tested end-to-end by agents against the real running system:
- If it has a UI, `nightmare-tester` tests both logic and UI via Playwright against the real app
- If it doesn't have a UI (new service, Celery task, management command, Dockerfile, etc.), `logic-tester` tests it through Django shell, real Celery workers, shell commands, or direct service calls against the real database/Redis
- **NEVER write pytest/unittest test files. NEVER use mocks or patches.**
- The test result should be passed to me for final review: what was tested, what errors were found, and what was fixed
- **All features must be E2E tested by agents before asking for my review**
