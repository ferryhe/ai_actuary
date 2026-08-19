# Source Data Folder

Place local actuarial source files here while developing or running the workbench. Files in this folder are intentionally ignored by Git because they may contain confidential portfolio data. Keep committed, anonymized test fixtures under `tests/fixtures/` instead.

The planned v1 file format is UTF-8 CSV with one row per origin/development cell:

```csv
origin,development,value
2022,12,100000
2022,24,150000
2023,12,120000
```

- `origin` identifies the accident, policy, or underwriting period.
- `development` identifies the development age, such as 12, 24, or 36 months.
- `value` is a finite numeric paid or incurred amount.

Rows must have unique `origin`/`development` pairs. When the run is marked `cumulative: true`, decreasing values produce a validation warning because recoveries or corrections may be legitimate; an optional strict mode may reject them.

At present, the console accepts a built-in `sample_name` only. The API already accepts the same data as `triangle_rows`; the branch plan adds a safe `source_file` input that resolves a CSV relative to this folder. Do not use an absolute path or place secrets in a data file.

For a file-backed run, the workbench will read the CSV once and retain an immutable copy inside the local run artifact root. Rerun and replay use that snapshot instead of rereading this mutable folder. The console shows only a bounded input preview, row count, column names, and checksum; it does not load the entire source file into the page.
