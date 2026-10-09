# Capability packs added by `python main.py evolve`

Each accepted upgrade lands here as `packs/<name>/` (a `pack.json` and its
`templates/`), together with one entry appended to `../registry.json`, its
dependencies appended to `requirements-packs.txt` at the repository root and a
line in `../ledger.jsonl`. Nothing outside those four places is ever written,
and every core file is pinned by sha256 in `../core_manifest.json`.
