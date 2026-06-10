"""Figure manifest schema (FIG-MANIFEST-1).

The figure_manifest is an out-of-catalog JSON sidecar that indexes maraxiom FigureSidecar
InputPins (run_id + output_path + sha256), allowing maraxiom consumers to track provenance
at the campaign level without duplicating the InputPin sub-schema.

Key design principles:
1. Schema-free: This is NOT a DuckDB column; it's a declarative sidecar JSON file.
2. Provenance reconciliation: InputPin matches maraxiom's FigureSidecar.InputPin exactly.
3. Intent-focused: The manifest declares INTENT (which runs/data a figure derives from),
   not rendered artifacts. Rendering remains maraxiom's concern.
4. Render state tracking: Supports ready (fully rendered), deferred (intent pinned but
   rendering blocked by owner-only data), and empty (zero figures).

Path: <catalog>/sidecars/<campaign_id>/figure_manifest.json

Example usage (from maraxiom consumer):
    from bathos.figure_manifest import FigureManifest

    # Read manifest at end of campaign
    manifest = FigureManifest.read_manifest(
        Path("<catalog>/sidecars/camp_123/figure_manifest.json")
    )

    # Iterate over figures and their provenance
    for fig in manifest.figures:
        print(f"Figure: {fig.figure_id}")
        print(f"  Intent: {fig.intent}")
        print(f"  Render state: {fig.render_state}")
        for pin in fig.input_pins:
            print(f"    Data from run {pin.run_id}: {pin.output_path}")
            # Verify immutability via sha256
            assert pin.sha256 == compute_sha256(pin.output_path)
"""

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel


class RenderState(StrEnum):
    """Render state of a figure in the manifest."""

    READY = "ready"
    """Figure is fully rendered and available."""

    DEFERRED = "deferred"
    """Figure intent is pinned but rendering is blocked (e.g., needs owner-only data or styling)."""


class InputPin(BaseModel):
    """Provenance pin: references the data product this figure visualizes.

    This schema EXACTLY mirrors maraxiom.FigureSidecar.InputPin to ensure
    reconciliation without duplication.
    """

    run_id: str
    """Bathos run ID that produced the data product."""

    output_path: str
    """Path to the data file within the bathos catalog."""

    sha256: str
    """SHA256 hash of the data product (immutability guarantee)."""


class FigureEntry(BaseModel):
    """One figure entry in the manifest."""

    figure_id: str
    """Unique figure identifier (slug format)."""

    intent: str
    """Human-readable intent: what this figure is meant to show.
    Example: 'main result', 'supplementary ablation', 'owner-side comparison'."""

    input_pins: list[InputPin]
    """List of data sources (bathos run outputs) this figure derives from.
    Typically a single pin for analysis figures; may be multiple for comparisons."""

    render_state: RenderState
    """Render state: ready | deferred."""


class FigureManifest(BaseModel):
    """Figure manifest: declarative index of campaign figures and their provenance.

    This sidecar is emitted by bathos at campaign_conclude and consumed by maraxiom
    to scaffold deck seeds and track figure provenance at the campaign level.

    An empty campaign is expressed as figures=[] (an empty list), not a special render_state.
    """

    manifest_version: str
    """Schema version of this manifest (e.g., '1.0'). Used for backward-compatibility."""

    campaign_id: str
    """Campaign ID this manifest belongs to. Must match the sidecar path directory."""

    figures: list[FigureEntry]
    """List of figures in this campaign. Empty list is valid (no figures to render)."""

    def write_manifest(self, path: Path) -> None:
        """Write the manifest to a JSON sidecar file.

        Args:
            path: Path to write the figure_manifest.json file.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(self.model_dump_json(indent=2))

    @classmethod
    def read_manifest(cls, path: Path) -> "FigureManifest":
        """Read a manifest from a JSON sidecar file.

        Args:
            path: Path to the figure_manifest.json file.

        Returns:
            FigureManifest instance.

        Raises:
            FileNotFoundError: If the file does not exist.
            ValidationError: If the JSON is invalid or violates the schema.
        """
        path = Path(path)
        with open(path) as f:
            return cls.model_validate_json(f.read())
