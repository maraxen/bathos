You are reading the record of a software and research project. Everything you may use is in this directory:

READER_DIR

Read only files under that directory, using only the Read tool (Glob and Grep are also allowed, always with an explicit path under that directory). Do not open any other path. Do not use Bash or run commands, and write your output with the Write tool. Do not use any knowledge you may have about this project from elsewhere.

What is there:
- `index.json` lists the documents and sidecars you may cite, with their blob hashes.
- `runs.jsonl` has one tracked run per line.
- `git_log.jsonl` has one commit subject per line.
- `docs/<path>` holds decision records and specs, using each `path` in `index.json`. `sidecars/<blob_sha>/<path>` holds run pre-registrations, using each `blob_sha` and `path` in `index.json`.

Before you write anything, read the whole record: `runs.jsonl` and `git_log.jsonl` to the end, paging through them if they are long, and every doc and every sidecar listed in `index.json`. Your reading is only as good as what you read, so do not stop at a sample.

Task: say what this project appears to be doing, judging from this record alone.

Write a JSON file to OUTPUT_PATH with this shape:

{"packet_sha256": "PACKET_SHA", "reader_id": "READER_ID",
 "purposes": [{"id": "r1", "text": "...", "citations": ["...", "..."]}],
 "open_lines": [{"text": "...", "citations": ["..."]}]}

- `purposes`: each purpose the record shows work toward, as many or as few as the record shows. Name what each one is for, specifically enough that someone could disagree. "Developing tooling" is not specific enough. Each purpose needs at least 2 citations to different records.
- `open_lines`: lines of work with no visible purpose. Each needs at least 1 citation. This list may be empty.

Citation forms, which must point at something in this directory:
- `run:<id>`, or a unique prefix of at least 8 characters
- `git:<sha>`, at least 7 characters
- `sidecar:<path>`, with the path as listed in `index.json`
- `doc:<path>@<blob_sha>#Lx-Ly`, with the path and blob as listed in `index.json`, and a line range that exists in the file

Report only what the record supports. Write the file, then reply with one line: `done`.
