# WebSocket close codes sent as plain int

Date: 2026-10-31
Plan: inline (one-line production hotfix)

## What changed
- `projects/controller_consumer.py` (`controller_superseded`, `controller_force_disconnect`) and `visual_engine/consumers.py` (`vision_worker_superseded`): `self.close(code=int(CloseCode.…))` instead of the `IntEnum` member.
- `requirements.txt`: `autobahn~=26.10.2` pinned (it was only a transitive dependency of daphne).

## Why
The 2026-10-31 image rebuild pulled autobahn 26.10.2, whose `sendClose` checks `type(code) != int` and rejects `IntEnum`. Every supersede then crashed the consumer (1011 instead of 4001), so the replaced Test Runner reconnected and superseded the other one in a loop (270 errors in 12 minutes on production).

## Behaviour and contract changes
- none (close codes 4001/4002 on the wire are unchanged)

## Notes for future changes
- Always pass a plain `int` to `AsyncWebsocketConsumer.close(code=)`; enum members fail at runtime and mypy does not catch it.
