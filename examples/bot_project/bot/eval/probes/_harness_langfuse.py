"""Langfuse SDK dataset materialization for the self-hosted probe runner."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import anyio
from langfuse import Langfuse
from pydantic import BaseModel, ConfigDict, Field

from bot.eval.probes._harness_models import ExperimentItem, ExperimentSetup
from bot.eval.probes.schema import Probe, WorldSpec
from modex_agent.trace.experiment_attrs import stable_experiment_id

_SDK_TIMEOUT_SECONDS: Final = 15


class LangfuseExperimentConfig(BaseModel):
    """Credentials and dataset identity for F3 setup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    public_key: str = Field(min_length=1)
    secret_key: str = Field(min_length=1)
    dataset_name: str = Field(min_length=1)


class LangfuseExperimentRegistrar:
    """Create SDK dataset items, then mint the stable events-only run ID."""

    def __init__(self, config: LangfuseExperimentConfig) -> None:
        self._config = config

    async def __call__(self, world: WorldSpec, run_name: str) -> ExperimentSetup:
        with anyio.fail_after(_SDK_TIMEOUT_SECONDS + 2):
            return await anyio.to_thread.run_sync(
                self._prepare_sync,
                world,
                run_name,
                abandon_on_cancel=True,
            )

    def _prepare_sync(self, world: WorldSpec, run_name: str) -> ExperimentSetup:
        client = Langfuse(
            host=self._config.host,
            public_key=self._config.public_key,
            secret_key=self._config.secret_key,
            timeout=_SDK_TIMEOUT_SECONDS,
            tracing_enabled=False,
        )
        try:
            dataset = client.create_dataset(
                name=self._config.dataset_name,
                description=f"Frozen memory probe suite {world.suite_version}",
            )
            ingest = client.create_dataset_item(
                dataset_name=self._config.dataset_name,
                input={"phase": "ingest", "suite_version": world.suite_version},
                expected_output={"snapshot": True},
            )
            probe_items = [self._create_probe_item(client, probe) for probe in world.probes]
            experiment_id = stable_experiment_id(
                host=self._config.host,
                public_key=self._config.public_key,
                secret_key=self._config.secret_key,
                dataset_id=dataset.id,
                item_id=ingest.id,
                run_name=run_name,
            )
            return ExperimentSetup(
                dataset_id=dataset.id,
                experiment_id=experiment_id,
                experiment_name=run_name,
                ingest_item_id=ingest.id,
                probe_items=probe_items,
            )
        finally:
            client.shutdown()

    def _create_probe_item(self, client: Langfuse, probe: Probe) -> ExperimentItem:
        item = client.create_dataset_item(
            dataset_name=self._config.dataset_name,
            input={"probe_id": probe.probe_id, "question": probe.question},
            expected_output={"answers": probe.expected_answers},
            metadata={
                "probe_type": probe.probe_type.value,
                "suite_item": True,
            },
        )
        return ExperimentItem(probe_id=probe.probe_id, item_id=item.id)


def harness_source_hash() -> str:
    """Return the short implementation hash used in score provenance."""
    import hashlib

    path = Path(__file__).with_name("harness.py")
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]
