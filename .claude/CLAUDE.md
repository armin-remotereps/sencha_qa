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
