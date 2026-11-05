"""Consumer typing of the published SDK example (#541).

The first four lines after `agent` are the code block that #546 added to
docs/runtime-surfaces.md (v0.1.17 blob 358abd59), as written. The assert_type
lines state the types a consumer gets from the installed package. Checked with
`mypy --strict` against the installed wheel; never executed.
"""

from typing import assert_type

from openminion import Agent, AgentRunResult

agent: Agent[str, str] = Agent()

first = agent.run("Remember that I write code in Iosevka 14.")
if first.metadata.get("respond_kind") == "policy_confirmation_prompt":
    # Answer the confirmation on the thread that is waiting for it.
    answer = agent.run("yes", thread_id=first.thread_id)
    assert_type(answer, AgentRunResult[str])
    assert_type(answer.thread_id, str | None)

assert_type(first.thread_id, str | None)
streamed = agent.run_stream("yes", thread_id=first.thread_id, on_delta=print)
assert_type(streamed, AgentRunResult[str])
assert_type(streamed.thread_id, str | None)
default_routing = agent.run("yes", session_id=first.session_id)
assert_type(default_routing.thread_id, str | None)
