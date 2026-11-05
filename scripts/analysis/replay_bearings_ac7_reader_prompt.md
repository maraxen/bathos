You are reading the record of a software and research project. Everything you may use is in this directory:

READER_DIR

Read only files under that directory, using only the Read, Glob and Grep tools. Do not open any other path, do not run commands, and do not use any knowledge you may have about this project from elsewhere.

What is there:
- `index.json` lists the documents and sidecars you may cite, with their blob hashes.
- `runs.jsonl` has one tracked run per line.
- `git_log.jsonl` has one commit subject per line.
- `docs/` holds decision records and specs. `sidecars/` holds run pre-registrations.

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
