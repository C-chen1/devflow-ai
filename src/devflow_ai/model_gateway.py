from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID
from sqlalchemy.orm import Session, sessionmaker
from devflow_ai.coordinator import ModelTokenUsage, RunCoordinator
from devflow_ai.planner import (
    PlannerRawOutput, PlannerRepairRequest, build_planner_prompt, build_planner_repair_prompt,
)


@dataclass(frozen=True)
class ModelReply:
    output: dict[str, object] | str
    usage: ModelTokenUsage | None


class ModelGateway:
    """Reserve and settle one model request within its Run budget."""
    def __init__(
        self, *,
        session_factory: sessionmaker[Session],
        worker_id: str,
        estimate_input_tokens: Callable[[str], int],
        generate: Callable[[str, int], ModelReply],
        max_output_tokens: int,
    ):
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        self.session_factory = session_factory
        self.worker_id = worker_id
        self.estimate_input_tokens = estimate_input_tokens
        self.generate = generate
        self.max_output_tokens = max_output_tokens

    def request(
        self, run_id: UUID, *, agent_execution_id: UUID, prompt: str,
    ) -> dict[str, object] | str | None:
        input_tokens = self.estimate_input_tokens(prompt)
        with self.session_factory() as session:
            reservation = RunCoordinator(session).reserve_model_request(
                run_id, self.worker_id, agent_execution_id=agent_execution_id,
                estimated_input_tokens=input_tokens, max_output_tokens=self.max_output_tokens,
            )
        if reservation is None:
            return None

        # A timeout or missing usage leaves the committed reservation in place.
        reply = self.generate(prompt, reservation.max_output_tokens)
        if reply.usage is not None:
            with self.session_factory() as session:
                RunCoordinator(session).settle_model_request(
                    run_id, self.worker_id, agent_execution_id=agent_execution_id,
                    request_id=reservation.request_id, actual_usage=reply.usage,
                )
        return reply.output


class PlannerModelGateway(ModelGateway):
    def __init__(
        self, *, session_factory: sessionmaker[Session], worker_id: str,
        estimate_input_tokens: Callable[[str], int], generate: Callable[[str, int], ModelReply],
        max_output_tokens: int,
        build_initial_prompt: Callable[[dict[str, object]], str] = build_planner_prompt,
        build_repair_prompt: Callable[[PlannerRepairRequest], str] = build_planner_repair_prompt,
    ):
        super().__init__(
            session_factory=session_factory, worker_id=worker_id, estimate_input_tokens=estimate_input_tokens,
            generate=generate, max_output_tokens=max_output_tokens,
        )
        self.build_initial_prompt = build_initial_prompt
        self.build_repair_prompt = build_repair_prompt

    def initial(
        self, run_id: UUID, *, agent_execution_id: UUID, agent_input: dict[str, object],
    ) -> PlannerRawOutput | None:
        return self.request(
            run_id, agent_execution_id=agent_execution_id, prompt=self.build_initial_prompt(agent_input),
        )

    def repair(self, request: PlannerRepairRequest) -> PlannerRawOutput | None:
        return self.request(
            request.run_id, agent_execution_id=request.agent_execution_id, prompt=self.build_repair_prompt(request),
        )


class AdmissionModelGateway:
    """Request a model using Task admission budget; output is not an admission decision."""

    def __init__(self, *, session_factory: sessionmaker[Session],
                 estimate_input_tokens: Callable[[str], int],
                 generate: Callable[[str, int], ModelReply], max_output_tokens: int):
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        self.session_factory = session_factory
        self.estimate_input_tokens = estimate_input_tokens
        self.generate = generate
        self.max_output_tokens = max_output_tokens

    def request(self, task_id: UUID, *, prompt: str, claim=None) -> dict[str, object] | str | None:
        from devflow_ai.admission_budget import AdmissionBudgetService

        estimated_input = self.estimate_input_tokens(prompt)
        with self.session_factory() as session:
            reservation = AdmissionBudgetService(session).reserve(
                task_id, estimated_input_tokens=estimated_input, max_output_tokens=self.max_output_tokens, claim=claim,
            )
        if reservation is None:
            return None
        reply = self.generate(prompt, reservation.max_output_tokens)
        if reply.usage is not None:
            with self.session_factory() as session:
                AdmissionBudgetService(session).settle(
                    task_id, request_id=reservation.request_id, actual_usage=reply.usage,
                )
        return reply.output
