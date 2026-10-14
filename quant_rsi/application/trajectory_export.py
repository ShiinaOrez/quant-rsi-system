from quant_rsi.serialization.trajectory import encode_jsonl

from .storage_ports import TrajectoryRepository, ArtifactStore


def export_jsonl(repository: TrajectoryRepository, trajectory_id: str) -> bytes:
    return encode_jsonl(repository.get(trajectory_id))


def archive_export(repository: TrajectoryRepository, artifacts: ArtifactStore,
                   trajectory_id: str) -> str:
    """Any repository + any artifact backend; no SQLite or local-path branching."""
    return artifacts.put(export_jsonl(repository, trajectory_id))
