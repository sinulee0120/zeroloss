"""A node that answers is restarted gracefully; a hung one hard (2026-10-23 receiverPC:
Update PCs SIGKILLed the node twice within 4 s, once per module, both while starting)."""

from __future__ import annotations

from typing import Any

from agent.deployer.agent_link import AgentLink


class Service:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def restart(self) -> None:
        self.calls.append("hard")

    def graceful_restart(self) -> None:
        self.calls.append("graceful")


def link(answers: list[dict[str, Any] | None]) -> tuple[AgentLink, Service]:
    s = Service()
    a: Any = object.__new__(AgentLink)
    a.service, a.come_back_s = s, 2.0
    a.health = lambda timeout_s=1.0: answers.pop(0) if answers else {"boot_id": "new"}
    return a, s


def test_an_answering_node_stops_as_it_asks() -> None:
    a, s = link([{"boot_id": "old"}])
    assert a.restart() and s.calls == ["graceful"]


def test_a_node_that_does_not_answer_is_killed() -> None:
    a, s = link([None])
    assert a.restart() and s.calls == ["hard"]
