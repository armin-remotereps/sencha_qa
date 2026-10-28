from typing import ParamSpec, TypeVar

from asgiref.sync import SyncToAsync

_P = ParamSpec("_P")
_R = TypeVar("_R")

class DatabaseSyncToAsync(SyncToAsync[_P, _R]): ...

database_sync_to_async = DatabaseSyncToAsync

async def aclose_old_connections() -> None: ...
