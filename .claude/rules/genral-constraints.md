## General Constraints

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