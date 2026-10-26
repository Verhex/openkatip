# Turkish normalization evaluation

These optional tools evaluate Turkish text normalization against an operator-owned
corpus; no corpus or personal workstation paths are distributed. See each script's
`--help` for supported arguments. `fetch.py` explicitly downloads external datasets
when invoked; check their licenses, consent and storage before use. Routine tests
mock those transports and do not require fetching data.

Activate your Meet environment, set `PYTHONPATH=src`, and keep evaluation data in a
separate directory, for example `../katip-eval-data`. Windows users can set
`$env:PYTHONPATH = "$PWD/src"` and use `.venv/Scripts/python.exe`.

```sh
python scripts/tn_eval/fetch.py --help
python scripts/tn_eval/run.py --help
python scripts/tn_eval/compare.py --help
python -m pytest -q -p no:cacheprovider tests/test_tn_eval.py
```

Do not commit downloaded datasets, audio, private transcripts or benchmark exports.
Record dataset version and normalization backend with each result; avoid treating
local benchmark performance as a guarantee for real meeting audio.
