# HOTFIX: Resolve aiohttp conflict

This hotfix removes `aiohttp>=3.10.5` from `requirements.txt` so that
`line-bot-sdk==2.4.3` can install its compatible pinned dependency
`aiohttp==3.8.4` automatically.

## Steps
1) Replace your repo's `requirements.txt` with this version.
2) Render → Manual Deploy (or Create New Web Service)
3) Confirm build succeeds.

