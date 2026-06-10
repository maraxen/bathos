"""Test suite for figure_manifest sidecar schema (FIG-MANIFEST-1).

The figure_manifest is an out-of-catalog JSON sidecar that indexes maraxiom FigureSidecar
InputPins (run_id + output_path + sha256), allowing maraxiom consumers to track provenance
at the campaign level without duplicating the InputPin sub-schema.

Path: <catalog>/sidecars/<campaign_id>/figure_manifest.json
Schema: {manifest_version, campaign_id, figures: [{figure_id, intent, input_pins, render_state}]}
Render states: ready | deferred | empty (on empty figures list, manifest is still valid)
"""
import json
import tempfile
from pathlib import Path

from pydantic import ValidationError
from pytest import raises

from bathos.figure_manifest import FigureEntry, FigureManifest, InputPin, RenderState


class TestFigureManifestSchema:
    """Verify the figure_manifest schema validates correctly."""

    def test_manifest_creates_with_required_fields(self):
        """Given required fields, manifest initializes with no validation errors."""
        manifest = FigureManifest(
            manifest_version="1.0",
            campaign_id="campaign_abc123",
            figures=[],
        )
        assert manifest.manifest_version == "1.0"
        assert manifest.campaign_id == "campaign_abc123"
        assert manifest.figures == []

    def test_manifest_empty_figures_is_valid(self):
        """Given an empty figures list, manifest is valid (not a failure state)."""
        manifest = FigureManifest(
            manifest_version="1.0",
            campaign_id="camp_xyz",
            figures=[],
        )
        assert len(manifest.figures) == 0
        # Should serialize without error
        json_str = manifest.model_dump_json()
        assert "campaign_id" in json_str

    def test_figure_entry_with_ready_state(self):
        """Given a figure with render_state='ready', it validates."""
        pin = InputPin(
            run_id="run_123",
            output_path="outputs/figure_data.json",
            sha256="abc123def456",
        )
        figure = FigureEntry(
            figure_id="fig_001",
            intent="show main result",
            input_pins=[pin],
            render_state=RenderState.READY,
        )
        assert figure.figure_id == "fig_001"
        assert figure.render_state == RenderState.READY
        assert len(figure.input_pins) == 1
        assert figure.input_pins[0].run_id == "run_123"

    def test_figure_entry_with_deferred_state(self):
        """Given a figure with render_state='deferred', intent is pinned but render blocked."""
        pin = InputPin(
            run_id="run_456",
            output_path="outputs/sensitive_data.json",
            sha256="xyz789",
        )
        figure = FigureEntry(
            figure_id="fig_002",
            intent="owner-side visualization",
            input_pins=[pin],
            render_state=RenderState.DEFERRED,
        )
        assert figure.render_state == RenderState.DEFERRED
        # Intent is still clear even though rendering is deferred
        assert figure.intent == "owner-side visualization"

    def test_figure_entry_with_multiple_input_pins(self):
        """Given a figure with multiple input_pins, they all reconcile to InputPin schema."""
        pins = [
            InputPin(
                run_id="run_001",
                output_path="outputs/data1.json",
                sha256="hash1",
            ),
            InputPin(
                run_id="run_002",
                output_path="outputs/data2.json",
                sha256="hash2",
            ),
        ]
        figure = FigureEntry(
            figure_id="fig_combined",
            intent="multi-run comparison",
            input_pins=pins,
            render_state=RenderState.READY,
        )
        assert len(figure.input_pins) == 2
        assert figure.input_pins[0].run_id == "run_001"
        assert figure.input_pins[1].run_id == "run_002"

    def test_input_pin_reconciles_to_maraxiom_schema(self):
        """Given InputPin fields, they match maraxiom FigureSidecar.InputPin exactly."""
        # maraxiom InputPin has: run_id, output_path, sha256
        pin = InputPin(
            run_id="run_abc",
            output_path="path/to/data.json",
            sha256="de3f1a2b3c4d5e6f7a8b9c0d1e2f3a4b",
        )
        assert pin.run_id == "run_abc"
        assert pin.output_path == "path/to/data.json"
        assert pin.sha256 == "de3f1a2b3c4d5e6f7a8b9c0d1e2f3a4b"

    def test_figure_entry_with_optional_figure_kind(self):
        """Given a figure with figure_kind set, it is included in the manifest."""
        pin = InputPin(
            run_id="run_kind_test",
            output_path="outputs/analysis.json",
            sha256="kind_hash_123",
        )
        figure = FigureEntry(
            figure_id="fig_with_kind",
            intent="show analysis",
            input_pins=[pin],
            render_state=RenderState.READY,
            figure_kind="analysis_chart",
        )
        assert figure.figure_kind == "analysis_chart"

        # Round-trip through JSON
        json_str = figure.model_dump_json()
        assert "figure_kind" in json_str
        restored = FigureEntry.model_validate_json(json_str)
        assert restored.figure_kind == "analysis_chart"

    def test_figure_entry_without_figure_kind_backward_compat(self):
        """Given a figure without figure_kind (old JSON), it deserializes with kind=None."""
        pin = InputPin(
            run_id="run_old",
            output_path="outputs/old_data.json",
            sha256="old_hash",
        )
        figure = FigureEntry(
            figure_id="fig_no_kind",
            intent="old figure",
            input_pins=[pin],
            render_state=RenderState.READY,
        )
        assert figure.figure_kind is None

        # Serialize with exclude_none: None should be excluded (not "null" in JSON)
        json_str = figure.model_dump_json(exclude_none=True)
        parsed = json.loads(json_str)
        assert "figure_kind" not in parsed, "figure_kind None should be excluded from JSON"

        # Deserialize old JSON without figure_kind field
        old_json = json.dumps({
            "figure_id": "fig_old_format",
            "intent": "legacy figure",
            "input_pins": [{"run_id": "r1", "output_path": "p1", "sha256": "h1"}],
            "render_state": "ready",
        })
        restored = FigureEntry.model_validate_json(old_json)
        assert restored.figure_kind is None

    def test_figure_kind_none_excluded_from_json_roundtrip(self):
        """Given figure_kind=None, JSON serialization excludes the field, preserving backward compat."""
        pin = InputPin(
            run_id="run_1",
            output_path="out/data.json",
            sha256="hash123",
        )
        figure = FigureEntry(
            figure_id="fig_none_kind",
            intent="test",
            input_pins=[pin],
            render_state=RenderState.READY,
            figure_kind=None,  # Explicitly None
        )

        # Serialize with exclude_none to verify None is excluded
        json_str = figure.model_dump_json(exclude_none=True)
        parsed = json.loads(json_str)
        # figure_kind key should NOT appear in JSON when None
        assert "figure_kind" not in parsed

        # Manifest with such a figure should serialize without changing JSON bytes
        manifest = FigureManifest(
            manifest_version="1.0",
            campaign_id="camp_kind_none",
            figures=[figure],
        )
        # write_manifest uses exclude_none=True internally
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "manifest.json"
            manifest.write_manifest(path)
            with open(path) as f:
                manifest_json = f.read()
            parsed_manifest = json.loads(manifest_json)
            # Verify figures[0] does not have figure_kind key
            assert "figure_kind" not in parsed_manifest["figures"][0]


class TestFigureManifestSerialization:
    """Verify JSON serialization round-trips correctly."""

    def test_manifest_to_json(self):
        """Given a manifest, model_dump_json produces valid JSON."""
        pin = InputPin(
            run_id="run_1",
            output_path="out/fig.json",
            sha256="abc123",
        )
        figure = FigureEntry(
            figure_id="fig_1",
            intent="test figure",
            input_pins=[pin],
            render_state=RenderState.READY,
        )
        manifest = FigureManifest(
            manifest_version="1.0",
            campaign_id="camp_1",
            figures=[figure],
        )
        json_str = manifest.model_dump_json()
        parsed = json.loads(json_str)
        assert parsed["manifest_version"] == "1.0"
        assert parsed["campaign_id"] == "camp_1"
        assert len(parsed["figures"]) == 1
        assert parsed["figures"][0]["figure_id"] == "fig_1"

    def test_manifest_from_json(self):
        """Given JSON, model_validate_json reconstructs the manifest."""
        json_str = json.dumps(
            {
                "manifest_version": "1.0",
                "campaign_id": "camp_2",
                "figures": [
                    {
                        "figure_id": "fig_2",
                        "intent": "test reconstruction",
                        "input_pins": [
                            {
                                "run_id": "run_2",
                                "output_path": "out/data.json",
                                "sha256": "def456",
                            }
                        ],
                        "render_state": "ready",
                    }
                ],
            }
        )
        manifest = FigureManifest.model_validate_json(json_str)
        assert manifest.campaign_id == "camp_2"
        assert manifest.figures[0].figure_id == "fig_2"
        assert manifest.figures[0].render_state == RenderState.READY

    def test_empty_manifest_roundtrip(self):
        """Given an empty figures list, it serializes and deserializes correctly."""
        manifest = FigureManifest(
            manifest_version="1.0",
            campaign_id="camp_empty",
            figures=[],
        )
        json_str = manifest.model_dump_json()
        manifest_restored = FigureManifest.model_validate_json(json_str)
        assert manifest_restored.campaign_id == "camp_empty"
        assert len(manifest_restored.figures) == 0


class TestFigureManifestFileHandling:
    """Verify reading and writing manifest to disk."""

    def test_manifest_write_to_file(self):
        """Given a manifest, write_manifest creates a valid JSON sidecar file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            campaign_id = "camp_test"
            sidecar_dir = Path(tmpdir) / "sidecars" / campaign_id
            sidecar_dir.mkdir(parents=True, exist_ok=True)

            pin = InputPin(
                run_id="run_test",
                output_path="out/fig.json",
                sha256="hash_test",
            )
            figure = FigureEntry(
                figure_id="fig_test",
                intent="write test",
                input_pins=[pin],
                render_state=RenderState.READY,
            )
            manifest = FigureManifest(
                manifest_version="1.0",
                campaign_id=campaign_id,
                figures=[figure],
            )

            manifest_path = sidecar_dir / "figure_manifest.json"
            manifest.write_manifest(manifest_path)

            assert manifest_path.exists()
            with open(manifest_path) as f:
                data = json.load(f)
            assert data["campaign_id"] == campaign_id

    def test_manifest_read_from_file(self):
        """Given a manifest file, read_manifest reconstructs the manifest."""
        with tempfile.TemporaryDirectory() as tmpdir:
            campaign_id = "camp_read"
            sidecar_dir = Path(tmpdir) / "sidecars" / campaign_id
            sidecar_dir.mkdir(parents=True, exist_ok=True)

            manifest_path = sidecar_dir / "figure_manifest.json"
            manifest_data = {
                "manifest_version": "1.0",
                "campaign_id": campaign_id,
                "figures": [
                    {
                        "figure_id": "fig_read",
                        "intent": "read test",
                        "input_pins": [
                            {
                                "run_id": "run_read",
                                "output_path": "out/data.json",
                                "sha256": "hash_read",
                            }
                        ],
                        "render_state": "ready",
                    }
                ],
            }
            with open(manifest_path, "w") as f:
                json.dump(manifest_data, f)

            manifest = FigureManifest.read_manifest(manifest_path)
            assert manifest.campaign_id == campaign_id
            assert manifest.figures[0].figure_id == "fig_read"

    def test_manifest_roundtrip_via_file(self):
        """Given a manifest, write and read preserves all data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            campaign_id = "camp_roundtrip"
            sidecar_dir = Path(tmpdir) / "sidecars" / campaign_id
            manifest_path = sidecar_dir / "figure_manifest.json"

            pins = [
                InputPin(
                    run_id=f"run_{i}",
                    output_path=f"out/data_{i}.json",
                    sha256=f"hash_{i}",
                )
                for i in range(3)
            ]
            figures = [
                FigureEntry(
                    figure_id=f"fig_{i}",
                    intent=f"figure {i}",
                    input_pins=[pins[i]],
                    render_state=RenderState.READY if i < 2 else RenderState.DEFERRED,
                )
                for i in range(3)
            ]
            original = FigureManifest(
                manifest_version="1.0",
                campaign_id=campaign_id,
                figures=figures,
            )

            original.write_manifest(manifest_path)
            restored = FigureManifest.read_manifest(manifest_path)

            assert restored.manifest_version == original.manifest_version
            assert restored.campaign_id == original.campaign_id
            assert len(restored.figures) == len(original.figures)
            for orig_fig, rest_fig in zip(original.figures, restored.figures):
                assert rest_fig.figure_id == orig_fig.figure_id
                assert rest_fig.render_state == orig_fig.render_state
                assert len(rest_fig.input_pins) == len(orig_fig.input_pins)

    def test_manifest_roundtrip_with_figure_kind(self):
        """Given a manifest with figure_kind set, write and read preserves the kind."""
        with tempfile.TemporaryDirectory() as tmpdir:
            campaign_id = "camp_with_kinds"
            sidecar_dir = Path(tmpdir) / "sidecars" / campaign_id
            manifest_path = sidecar_dir / "figure_manifest.json"

            pin = InputPin(
                run_id="run_kind",
                output_path="out/analysis.json",
                sha256="hash_analysis",
            )
            figures = [
                FigureEntry(
                    figure_id="fig_chart",
                    intent="analysis chart",
                    input_pins=[pin],
                    render_state=RenderState.READY,
                    figure_kind="analysis_chart",
                ),
                FigureEntry(
                    figure_id="fig_struct",
                    intent="structural diagram",
                    input_pins=[pin],
                    render_state=RenderState.READY,
                    figure_kind="structural",
                ),
                FigureEntry(
                    figure_id="fig_legacy",
                    intent="legacy figure",
                    input_pins=[pin],
                    render_state=RenderState.READY,
                    figure_kind=None,  # No kind set
                ),
            ]
            original = FigureManifest(
                manifest_version="1.0",
                campaign_id=campaign_id,
                figures=figures,
            )

            original.write_manifest(manifest_path)
            restored = FigureManifest.read_manifest(manifest_path)

            assert len(restored.figures) == 3
            assert restored.figures[0].figure_kind == "analysis_chart"
            assert restored.figures[1].figure_kind == "structural"
            assert restored.figures[2].figure_kind is None

            # Verify JSON excludes None values
            with open(manifest_path) as f:
                json_text = f.read()
                parsed = json.loads(json_text)
            assert "figure_kind" in parsed["figures"][0]
            assert parsed["figures"][0]["figure_kind"] == "analysis_chart"
            assert "figure_kind" in parsed["figures"][1]
            assert parsed["figures"][1]["figure_kind"] == "structural"
            assert "figure_kind" not in parsed["figures"][2], "None figure_kind should not appear in JSON"


class TestRenderStateEnum:
    """Verify render_state enum is correct."""

    def test_render_state_ready(self):
        """Given render_state='ready', figure is fully rendered and available."""
        assert RenderState.READY.value == "ready"

    def test_render_state_deferred(self):
        """Given render_state='deferred', figure intent is pinned but rendering blocked."""
        assert RenderState.DEFERRED.value == "deferred"

    def test_render_state_in_figure_entry(self):
        """Given a figure, render_state is one of the valid enum values."""
        for state in [RenderState.READY, RenderState.DEFERRED]:
            pin = InputPin(
                run_id="run_1",
                output_path="out/fig.json",
                sha256="hash_1",
            )
            figure = FigureEntry(
                figure_id="fig_1",
                intent="test",
                input_pins=[pin],
                render_state=state,
            )
            assert figure.render_state in [RenderState.READY, RenderState.DEFERRED]

    def test_empty_campaign_uses_empty_figures_list(self):
        """Given a campaign with no figures, it is expressed as figures=[] (empty list)."""
        # Empty campaigns do not use a special render_state; they are manifests with zero entries.
        manifest = FigureManifest(
            manifest_version="1.0",
            campaign_id="camp_empty_test",
            figures=[],
        )
        assert len(manifest.figures) == 0
        # Should still serialize and deserialize correctly
        json_str = manifest.model_dump_json()
        restored = FigureManifest.model_validate_json(json_str)
        assert len(restored.figures) == 0
