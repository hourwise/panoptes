"""Common interface for agent-harness adapters (OMP today; Pi, opencode,
Codex CLI, Claude Code later).

Every adapter exposes ``snapshot()`` returning one plain-JSON dict with this
schema. Keys whose data a harness cannot provide are present but ``None``;
``capabilities`` says which sections the harness could *ever* fill, so the UI
can hide a section instead of showing an empty one.

    id                  config id, e.g. "omp-local"
    type                harness type from the config, e.g. "omp"
    label               display label from the config
    status              "working" | "idle" | "unknown"
                        ("unknown" when the harness cannot observe activity)
    active_task         {id, started_at, elapsed_s} while a task holds the
                        worker lock, else None
    session             the live/most-recent session, or None when the
                        harness has no session files:
      file              basename only, never a path
      started_at        ISO timestamp of the first record seen
      last_activity_at  ISO timestamp of the last record seen
      turns             number of user/assistant messages
      input_tokens      sum of usage.input over assistant messages
      output_tokens     sum of usage.output
      reasoning_tokens  sum of usage.reasoningTokens
      current_context   input + cacheRead of the LAST assistant message
      peak_context      max(input + cacheRead) over the session
      compactions       number of compaction records
      tool_calls        {tool name: count}
      failed_tool_calls messages with role "toolResult" and isError true
      aborted_requests  assistant messages with stopReason "aborted"
      model             model of the last assistant message
    capabilities        dict of booleans, keys below
    warnings            list[str] of anything odd seen this snapshot

PRIVACY: a snapshot never carries message text, reasoning, tool arguments,
file contents or code — only counts, tool names, sizes and timestamps. The
privacy test in ``tests/test_harness.py`` enforces this on fixtures.

``SCHEMA`` lists the exact keys of each grouped section, verbatim; the UI
indexes them literally, so an adapter that hands back the harness's raw
record names gives a card that renders empty.
"""

# "errors" covers the two error signals OMP actually records: tool results
# flagged isError (failed_tool_calls) and assistant messages cut short with
# stopReason "aborted" (aborted_requests). Neither is a retry: OMP writes no
# retry record at all, so nothing here — API or UI — may be labelled one.
CAPABILITIES = (
    "live_session",   # a session file that grows while the worker runs
    "tokens",         # per-message usage accounting (input/output/reasoning)
    "compactions",    # compaction records are visible
    "tool_calls",     # tool call names and counts
    "errors",         # failed tool calls and aborted requests (never "retries")
)

# section -> the exact keys that section must contain, in this order.
SCHEMA = {
    "active_task": ("id", "started_at", "elapsed_s"),
    "session": ("file", "started_at", "last_activity_at", "turns",
                "input_tokens", "output_tokens", "reasoning_tokens",
                "current_context", "peak_context", "compactions",
                "tool_calls", "failed_tool_calls", "aborted_requests", "model"),
    "capabilities": CAPABILITIES,
}

# The keys of a top-level snapshot, in this order.
TOP_LEVEL = ("id", "type", "label", "status", "active_task", "session",
             "capabilities", "warnings")

STATUSES = ("working", "idle", "unknown")


def empty_section(section: str) -> dict:
    """A snapshot section with exactly the SCHEMA keys, every value None."""
    return {key: None for key in SCHEMA[section]}


def empty_capabilities() -> dict:
    """All capabilities False; adapters flip the ones they support."""
    return {key: False for key in CAPABILITIES}


class HarnessAdapter:
    """One agent harness the dashboard watches. Subclasses fill the schema."""

    type = ""

    def __init__(self, id: str = "", label: str = ""):
        self.id = id
        self.label = label or id

    def snapshot(self) -> dict:
        raise NotImplementedError
