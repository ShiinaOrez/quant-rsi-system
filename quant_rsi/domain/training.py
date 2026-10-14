from dataclasses import dataclass
from enum import StrEnum

from .benchmark import DatasetSplit
from .shared import require


class TrainingMethod(StrEnum):
    SFT = "sft"
    DPO = "dpo"
    RL = "rl"


@dataclass(frozen=True)
class TrajectoryReference:
    trajectory_id: str
    task_id: str
    split: DatasetSplit
    configuration_digest: str
    data_digest: str
    policy_revision: str

    def __post_init__(self):
        for value in (self.trajectory_id, self.task_id, self.policy_revision):
            require(bool(value.strip()), "trajectory provenance required")
        require(isinstance(self.split, DatasetSplit), "typed split required")
        for digest in (self.configuration_digest, self.data_digest):
            require(len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
                    "trajectory configuration and data SHA-256 required")


@dataclass(frozen=True)
class PostTrainingPlan:
    run_id: str
    base_model: str
    method: TrainingMethod
    reward_revision: str
    trajectories: tuple[TrajectoryReference, ...]

    def __post_init__(self):
        require(bool(self.run_id) and bool(self.base_model) and bool(self.reward_revision),
                "training identity and objective revision required")
        require(isinstance(self.method, TrainingMethod), "typed training method required")
        require(type(self.trajectories) is tuple and bool(self.trajectories),
                "immutable nonempty training references required")
        require(all(t.split == DatasetSplit.TRAIN for t in self.trajectories),
                "validation/test trajectories cannot enter post-training")
        require(len({t.trajectory_id for t in self.trajectories}) == len(self.trajectories),
                "duplicate trajectory references")
