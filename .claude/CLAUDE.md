# CLAUDE.md

## Project overview

We want to create a system to run test cases for sencha project.

## Project big picture

A user will upload testrails xml file which contains multiple test cases, and we want to do following for each of them:

1. Create a isolated environment using playwright, docker, ssh, and vnc
2. Do the test case on that environment using ai (DMR)
3. Save the test result on the panel

## Project stacks

- Python3.13
- NO pyproject.toml
- Django
- Celery (django integrated)
- Postgresql
- Docker client
- VNC client
- Playwright client
- DMR client (Docker Model Runner)
- ssh client
- Mypy (strict mode)
- black
- isort
- pre commit (with mypy, black, and isort on it)
- django channels
- Alpine JS (for frontend interactive stuff)
- SHADCN (UI)

## Constraints

1. Typing is super important, no typing skipping
2. do NOT use apis as much as possible, pass data to django and use django template engine, use alpine js for socket, and some js stuff you may need like hamburger menu, ...
3. Use custom user model, we may need to expand it
4. Implement both static and media prefix and root on settings.py
5. no direct use of env on project, all of them should be defined on settings.py
6. Celery should be configured and integrated with django using django_backend, django celery beat, ...
7. Tasks and View shouldn't implement any business logic, they should call service layer for logic handling. They are allowed to do only data manipulation like dto -> model, ....
8. Celery broker is redis
9. All configs for mdr, db, redis, ... should be on .env
10. example.env should always be up to date
11. `SUPER IMPORTANT` when installing a package, don't use `pip install x` then `pip freeze > requirements.txt`. Instead, find the package on pypi, and put it on the requirements.txt like this: `x~=1.0.1` then do `pip install -r requirements.txt`
12. Always put your plan on the project root, then after the feature is done, move it to docs
13. DO NOT use lazy imports as much as possible, put all imports at top of the file

## Task implementation flow:

1. I will provide you a task, feature, or a bug. It is on a file on directory docs/specs
2. You will plan it using plan mode, will ask any technical or business questions from me (I'm a staff engineer so I can answer all your questions)
3. After my confirmation, you will create plan
4. You will spawn parallel agents to implement logics, frontend, views, tasks, ... . Don't forget to use `dmr-agent-architect`, `frontend-craftsmand`, `django-view-architect`, and `celery-architect` agents for them! (note: each agent should write its own test cases before starting main implementation, so they should create their function and classes structure first, then test cases (they should fail) then the main logic which passes the test cases)
5. After implementation is done, you'll check if tests are passing or not, if not, resolve the issue, wether the test has problem or the logic
6. After that, you'll use playwright to test the implemented task using `nightmare-tester` agent! (Also don't forget to delete screenshots and test user after testing is done). In parallel you run mypy strict mode for any errors and ask `uncle bob` for any feedback on code
9. After fixing all previous steps bugs, time to ask me for final testing
10. After I confirmed, you'll add the implementation doc on `docs/impl`. For example if I passed you `docs/specs/001.feat-x.md`, you will create `docs/impl/001.feat-x-impl.md`
