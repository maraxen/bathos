from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pyarrow as pa

COOL_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("project_slug", pa.string()),
        pa.field("command", pa.string()),
        pa.field("argv", pa.list_(pa.string())),
        pa.field("git_hash", pa.string()),
        pa.field("git_branch", pa.string()),
        pa.field("git_dirty", pa.bool_()),
        pa.field("timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("duration_s", pa.float64()),
        pa.field("exit_code", pa.int32()),
        pa.field("status", pa.string()),
        pa.field("output_paths", pa.list_(pa.string())),
        pa.field("tags", pa.list_(pa.string())),
        pa.field("schema_version", pa.string()),
        pa.field("slurm_job_id", pa.string()),
        pa.field("hostname", pa.string()),
    ]
)

WARM_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("project_slug", pa.string()),
        pa.field("command", pa.string()),
        pa.field("argv", pa.list_(pa.string())),
        pa.field("git_hash", pa.string()),
        pa.field("git_branch", pa.string()),
        pa.field("git_dirty", pa.bool_()),
        pa.field("timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("duration_s", pa.float64()),
        pa.field("exit_code", pa.int32()),
        pa.field("status", pa.string()),
        pa.field("output_paths", pa.list_(pa.string())),
        pa.field("tags", pa.list_(pa.string())),
        pa.field("schema_version", pa.string()),
        pa.field("slurm_job_id", pa.string()),
        pa.field("hostname", pa.string()),
        pa.field("metadata", pa.string()),
        pa.field("outcome", pa.string()),
        pa.field("output_metadata", pa.string()),
    ]
)


@dataclass
class Run:
    project_slug: str
    command: str
    argv: list[str]
    git_hash: str
    git_branch: str
    git_dirty: bool
    id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_s: float = 0.0
    exit_code: int = -1
    status: str = "running"
    output_paths: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    schema_version: str = "2"
    slurm_job_id: str = ""
    hostname: str = ""
    metadata: str = "{}"

    def to_arrow(self) -> pa.Table:
        return pa.table(
            {
                "id": [self.id],
                "project_slug": [self.project_slug],
                "command": [self.command],
                "argv": [self.argv],
                "git_hash": [self.git_hash],
                "git_branch": [self.git_branch],
                "git_dirty": [self.git_dirty],
                "timestamp": pa.array([self.timestamp], type=pa.timestamp("us", tz="UTC")),
                "duration_s": [self.duration_s],
                "exit_code": [self.exit_code],
                "status": [self.status],
                "output_paths": [self.output_paths],
                "tags": [self.tags],
                "schema_version": [self.schema_version],
                "slurm_job_id": [self.slurm_job_id],
                "hostname": [self.hostname],
            },
            schema=COOL_SCHEMA,
        )

    @classmethod
    def from_arrow_row(cls, pydict: dict, i: int) -> Run:
        ts = pydict["timestamp"][i]
        if not isinstance(ts, datetime):
            ts = ts.as_py()
        return cls(
            id=pydict["id"][i],
            project_slug=pydict["project_slug"][i],
            command=pydict["command"][i],
            argv=list(pydict["argv"][i]),
            git_hash=pydict["git_hash"][i],
            git_branch=pydict["git_branch"][i],
            git_dirty=bool(pydict["git_dirty"][i]),
            timestamp=ts,
            duration_s=float(pydict["duration_s"][i]),
            exit_code=int(pydict["exit_code"][i]),
            status=pydict["status"][i],
            output_paths=list(pydict["output_paths"][i]),
            tags=list(pydict["tags"][i]),
            schema_version=pydict.get("schema_version", ["1"])[i]
            if "schema_version" in pydict
            else "1",
            slurm_job_id=pydict.get("slurm_job_id", [""])[i] if "slurm_job_id" in pydict else "",
            hostname=pydict.get("hostname", [""])[i] if "hostname" in pydict else "",
        )
