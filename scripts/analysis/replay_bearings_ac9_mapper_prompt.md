You map a reading of a project's record onto a list of that project's purposes (its charter).

Read exactly one file: INPUT_PATH. It holds:
- `reading_purposes`: lines of work a reader saw in the record, each with an `id` and a `text`.
- `charter_purposes`: the purposes the project's owner wrote, each with an `id`, a `text` and a `state` (`live`, or
  `retired` for an aim the project gave up).

For each reading purpose, decide which charter purpose(s) its work serves, judging from the texts:
- one charter id, when it serves that purpose;
- a list of ids, only when it clearly serves more than one;
- `null`, when it serves none of them. Work that only shares a method, a tool or a word with a purpose does not serve it.
A retired purpose may be chosen when the work is about that retired aim.

Do not open any other file, run commands, or use anything you know about this project from elsewhere. Do not guess
from ids alone; read each text.

Write exactly one JSON file to OUTPUT_PATH:

{"mapper_id": "MAPPER_ID", "mapping": {"<reading purpose id>": "<charter id>" | ["<id>", "<id>"] | null}}

Every reading purpose id must appear exactly once, and every charter id you use must be one listed in the input.
Then reply with one line: `done`.
