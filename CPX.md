# CPX fork notes

This is Commit-Push-Pray's fork of `git@github.com:Continuum-AI-Corp/OrcaRouter-Lite.git`, used by CPX-GenAI.

- `main` mirrors upstream and is never edited here.
- `cpx` is our working branch (default); merge `upstream/main` into it to take upstream changes.
- Original license and notices are unchanged. Files we modify are marked in the commit history.

## Changes in this fork
- Image and video generation endpoints (`/v1/images/generations`, `/v1/videos`, `/v1/media/*`), a media job table, a provider interface with a fake provider, budget checks and a background sweeper. See [CPX-MEDIA.md](CPX-MEDIA.md). Real providers are still to come.
