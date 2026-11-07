# MCP gateway: store hand-in screenshots on the shared media volume

Date: 2026-11-08
Plan: inline (one-line fix)

## What changed
- `docker-compose.yml`: the `mcp` service mounts `MEDIA:/src/media`, like the api, the celery workers and nginx.

## Why
`hand_in` writes the manual screenshots under `MEDIA_ROOT`. Without the volume, the files landed in the `mcp` container's own filesystem: the database rows existed, but api and nginx could not serve the images (broken image on the case page), and a container recreate deleted them. One shot was affected (run 158, case 2986). It was copied into the volume by hand before the redeploy.

## Behaviour and contract changes
- none

## Notes for future changes
- Every service that writes files through Django storage needs the matching volume. Check `docker-compose.yml` when a new process starts writing media.
