"""Claude Code hook entry points for bathos.

Each module here is a console script. It imports only the standard library at load time, because
it runs before tool calls, where bathos's own startup cost (cyclopts, duckdb, pyarrow) is too high.
"""
