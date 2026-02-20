## Task implementation flow:

1. I will provide you a task, feature, or a bug. It is on a file (or directory for big features) on directory docs/specs
2. You will plan it using plan mode, will ask any technical or business questions from me (I'm a staff engineer so I can answer all your questions)
3. After my confirmation, you will create plan
4. You will spawn parallel agents to implement logics, frontend, views, tasks, ... . Don't forget to use `dmr-agent-architect`, `frontend-craftsman`, `django-view-architect`, and `celery-architect` agents for them!
5. After implementation is done, you'll E2E test using `nightmare-tester` (for UI features) or `logic-tester` (for backend-only features) or both. Also don't forget to delete screenshots and test user after testing is done. In parallel you run mypy strict mode for any errors and ask `uncle bob` for any feedback on code
9. After fixing all previous steps bugs, time to ask me for final testing
10. After I confirmed, you'll add the implementation doc on `docs/impl`. For example if I passed you `docs/specs/001.feat-x.md`, you will create `docs/impl/001.feat-x-impl.md`