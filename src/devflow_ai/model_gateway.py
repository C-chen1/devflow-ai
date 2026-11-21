from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID
from sqlalchemy.orm import Session, sessionmaker
from devflow_ai.coordinator import ModelTokenUsage, RunCoordinator
from devflow_ai.experiment import ExperimentLimitError
from devflow_ai.planner import (
    PlannerRawOutput, PlannerRepairRequest, build_planner_prompt, build_planner_repair_prompt,
)


@dataclass(frozen=True)
class ModelReply:
    output: dict[str, object] | str
    usage: ModelTokenUsage | None


class ModelGateway:
    """Reserve and settle one model request within its Run budget.

    ``experiment_budget`` adds an outer, experiment-wide reservation (see
    :mod:`devflow_ai.experiment_budget`). It is opt-in: without a hook this class behaves
    exactly as before. With one, the request is sent only after both reservations succeeded,
    and the outer reservation is released only when no call could have been made.
    """
    def __init__(
        self, *,
        session_factory: sessionmaker[Session],
        worker_id: str,
        estimate_input_tokens: Callable[[str], int],
        generate: Callable[[str, int], ModelReply],
        max_output_tokens: int,
        experiment_budget=None,
    ):
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        self.session_factory = session_factory
        self.worker_id = worker_id
        self.estimate_input_tokens = estimate_input_tokens
        self.generate = generate
        self.max_output_tokens = max_output_tokens
        self.experiment_budget = experiment_budget

    def request(
        self, run_id: UUID, *, agent_execution_id: UUID, prompt: str,
    ) -> dict[str, object] | str | None:
        input_tokens = self.estimate_input_tokens(prompt)
        # Outer layer first: it is the coarser ceiling and taking it before the Run row lock
        # keeps one lock order (experiment, then Run) for every caller of this gateway.
        experiment_key = None
        if self.experiment_budget is not None:
            experiment_key = self.experiment_budget.open_request(
                estimated_tokens=input_tokens + self.max_output_tokens,
                run_id=run_id, agent_execution_id=agent_execution_id)
            if experiment_key is None:
                return None
        try:
            with self.session_factory() as session:
                reservation = RunCoordinator(session).reserve_model_request(
                    run_id, self.worker_id, agent_execution_id=agent_execution_id,
                    estimated_input_tokens=input_tokens,
                    max_output_tokens=self.max_output_tokens,
                )
        except BaseException as error:
            # The request was never sent, so the outer reservation can be given back.
            self._release_experiment(
                experiment_key, f"the Run budget raised {type(error).__name__}")
            raise
        if reservation is None:
            self._release_experiment(experiment_key, "the Run budget had no room")
            return None

        if experiment_key is not None:
            # Past this line the request may reach the provider; releasing is no longer allowed.
            try:
                self.experiment_budget.mark_dispatched(experiment_key)
            except ExperimentLimitError as error:
                # Another request may already be on its way. This one has not been sent, so its
                # outer reservation goes back; the Run reservation stays as a conservative hold.
                self._release_experiment(experiment_key, f"dispatch refused: {error}")
                return None
        try:
            reply = self.generate(prompt, reservation.max_output_tokens)
        except BaseException as error:
            # The call may have reached the provider: unknown, never zero, never released.
            self._mark_experiment_unknown(
                experiment_key, f"model call raised {type(error).__name__}")
            raise

        if experiment_key is not None:
            if reply.usage is None:
                self._mark_experiment_unknown(
                    experiment_key, "the provider returned no usage")
            else:
                # The aggregate ceiling is settled first: if this fails, the Run reservation
                # stays in place and the experiment reports incomplete accounting.
                self.experiment_budget.settle(experiment_key, reply.usage)
        if reply.usage is not None:
            with self.session_factory() as session:
                RunCoordinator(session).settle_model_request(
                    run_id, self.worker_id, agent_execution_id=agent_execution_id,
                    request_id=reservation.request_id, actual_usage=reply.usage,
                )
        return reply.output

    def _release_experiment(self, request_key, reason: str) -> None:
        if request_key is None:
            return
        try:
            self.experiment_budget.release(request_key, reason)
        except Exception:  # best effort: the reservation stays held, which is the safe side
            pass

    def _mark_experiment_unknown(self, request_key, reason: str) -> None:
        if request_key is None:
            return
        try:
            self.experiment_budget.mark_unknown(request_key, reason)
        except Exception:  # best effort: the reservation stays held, which is the safe side
            pass


class PlannerModelGateway(ModelGateway):
    def __init__(
        self, *, session_factory: sessionmaker[Session], worker_id: str,
        estimate_input_tokens: Callable[[str], int], generate: Callable[[str, int], ModelReply],
        max_output_tokens: int,
        build_initial_prompt: Callable[[dict[str, object]], str] = build_planner_prompt,
        build_repair_prompt: Callable[[PlannerRepairRequest], str] = build_planner_repair_prompt,
        experiment_budget=None,
    ):
        super().__init__(
            session_factory=session_factory, worker_id=worker_id, estimate_input_tokens=estimate_input_tokens,
            generate=generate, max_output_tokens=max_output_tokens,
            experiment_budget=experiment_budget,
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
    """Request a model using Task admission budget; output is not an admission decision.

    ``experiment_budget`` works the same way as in :class:`ModelGateway`: an opt-in outer
    reservation, taken before the admission budget and released only when no call was made.
    An admission request has no Run, so the outer reservation records none.
    """

    def __init__(self, *, session_factory: sessionmaker[Session],
                 estimate_input_tokens: Callable[[str], int],
                 generate: Callable[[str, int], ModelReply], max_output_tokens: int,
                 experiment_budget=None):
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        self.session_factory = session_factory
        self.estimate_input_tokens = estimate_input_tokens
        self.generate = generate
        self.max_output_tokens = max_output_tokens
        self.experiment_budget = experiment_budget

    def request(self, task_id: UUID, *, prompt: str, claim=None) -> dict[str, object] | str | None:
        from devflow_ai.admission_budget import AdmissionBudgetService

        estimated_input = self.estimate_input_tokens(prompt)
        experiment_key = None
        if self.experiment_budget is not None:
            experiment_key = self.experiment_budget.open_request(
                estimated_tokens=estimated_input + self.max_output_tokens)
            if experiment_key is None:
                return None
        try:
            with self.session_factory() as session:
                reservation = AdmissionBudgetService(session).reserve(
                    task_id, estimated_input_tokens=estimated_input,
                    max_output_tokens=self.max_output_tokens, claim=claim)
        except BaseException as error:
            self._release_experiment(
                experiment_key, f"the admission budget raised {type(error).__name__}")
            raise
        if reservation is None:
            self._release_experiment(experiment_key, "the admission budget had no room")
            return None
        if experiment_key is not None:
            try:
                self.experiment_budget.mark_dispatched(experiment_key)
            except ExperimentLimitError as error:
                self._release_experiment(experiment_key, f"dispatch refused: {error}")
                return None
        try:
            reply = self.generate(prompt, reservation.max_output_tokens)
        except BaseException as error:
            self._mark_experiment_unknown(
                experiment_key, f"model call raised {type(error).__name__}")
            raise
        if experiment_key is not None:
            if reply.usage is None:
                self._mark_experiment_unknown(experiment_key, "the provider returned no usage")
            else:
                self.experiment_budget.settle(experiment_key, reply.usage)
        if reply.usage is not None:
            with self.session_factory() as session:
                AdmissionBudgetService(session).settle(
                    task_id, request_id=reservation.request_id, actual_usage=reply.usage)
        return reply.output

    def _release_experiment(self, request_key, reason: str) -> None:
        if request_key is None:
            return
        try:
            self.experiment_budget.release(request_key, reason)
        except Exception:
            pass

    def _mark_experiment_unknown(self, request_key, reason: str) -> None:
        if request_key is None:
            return
        try:
            self.experiment_budget.mark_unknown(request_key, reason)
        except Exception:
            pass
