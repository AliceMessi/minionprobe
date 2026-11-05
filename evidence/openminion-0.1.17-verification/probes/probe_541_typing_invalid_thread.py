"""Consumer typing (#541): `thread_id` is typed `str | None`; an int is one error."""

from openminion import Agent

agent: Agent[str, str] = Agent()
agent.run("yes", thread_id=1)
