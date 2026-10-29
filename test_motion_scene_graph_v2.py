import unittest
from pathlib import Path

from pydantic import ValidationError

from motion.models import MotionPlan
from motion.scene_graph_v2 import (
    GraphLayer,
    GraphRect,
    MotionPlanV2,
    MotionTrack,
    SceneGraph,
    StyleBible,
)
from motion.skills.registry import load_registry, route_skills


def graph_fixture():
    return MotionPlanV2(
        source_job_id="fixture_job",
        source_fingerprint="sha256:" + "a" * 64,
        revision=3,
        duration_ms=5000,
        output={"width": 720, "height": 1280, "fps_num": 30, "fps_den": 1},
        style_bible={"style_id": "editorial-kinetic", "accent": "#F4C95D", "ink": "#FFFFFF"},
        scenes=[SceneGraph(
            scene_id="process-a",
            start_ms=1000,
            end_ms=3500,
            semantic_goal="Show a process as sequential states.",
            spoken_evidence="сначала проверяем исходник",
            skills_used=["video-kinetic-typography", "video-graphics"],
            placement={"x": 0.5, "y": 0.22, "w": 0.72, "h": 0.12, "anchor": "center"},
            layers=[
                GraphLayer(
                    id="title", kind="text", text="ИСХОДНИК", color="ink",
                    geometry=GraphRect(x=0.08, y=0.08, w=0.72, h=0.48),
                ),
                GraphLayer(
                    id="line", kind="line", shape="underline", color="accent",
                    geometry=GraphRect(x=0.08, y=0.78, w=0.84, h=0.1),
                ),
            ],
            tracks=[
                MotionTrack(layer_id="title", property="opacity", keyframes=[
                    {"at_ms": 0, "value": 0}, {"at_ms": 260, "value": 1},
                ]),
                MotionTrack(layer_id="line", property="draw_progress", keyframes=[
                    {"at_ms": 160, "value": 0}, {"at_ms": 620, "value": 1},
                ]),
                MotionTrack(layer_id="title", property="x", keyframes=[
                    {"at_ms": 0, "value": -0.08}, {"at_ms": 360, "value": 0.02},
                ]),
            ],
        )],
    )


class MotionSceneGraphV2Tests(unittest.TestCase):
    def test_typed_graph_compiles_to_the_legacy_remotion_contract(self):
        graph = graph_fixture()
        compiled = graph.compile_legacy_plan()
        loaded = MotionPlan.model_validate(compiled.model_dump(mode="json"))
        scene = loaded.scenes[0]
        self.assertEqual(loaded.revision, 3)
        self.assertEqual(scene.effect_type, "kinetic_sequence")
        self.assertEqual(scene.content["motion_graph"]["version"], 2)
        self.assertEqual([track["property"] for track in scene.content["motion_graph"]["tracks"]], ["opacity", "draw_progress", "x"])

    def test_tracks_must_reference_known_layers_and_fit_scene_duration(self):
        with self.assertRaises(ValidationError):
            SceneGraph(
                scene_id="invalid", start_ms=100, end_ms=1000,
                semantic_goal="Invalid reference", placement={"x": 0.5, "y": 0.5, "w": 0.5, "h": 0.2},
                layers=[GraphLayer(id="shape", kind="shape", shape="circle", geometry={"x": 0, "y": 0, "w": 1, "h": 1})],
                tracks=[MotionTrack(layer_id="missing", property="scale", keyframes=[{"at_ms": 0, "value": 1}, {"at_ms": 100, "value": 1.2}])],
            )

        with self.assertRaises(ValidationError):
            MotionTrack(layer_id="shape", property="opacity", keyframes=[{"at_ms": 200, "value": 0}, {"at_ms": 100, "value": 1}])

    def test_generated_bounds_and_missing_line_primitive_are_normalized_safely(self):
        raw = graph_fixture().model_dump(mode="json")
        scene = raw["scenes"][0]
        scene["placement"].update({"x": 0.1, "w": 0.8, "y": 0.02, "h": 0.18})
        scene["layers"][1]["geometry"].update({"x": 0.9, "w": 0.5})
        scene["layers"][1].pop("shape")
        scene["tracks"].append({
            "layer_id": "title", "property": "x", "easing": "ease_out",
            "keyframes": [{"at_ms": 0, "value": -0.08}, {"at_ms": 360, "value": 0.02}],
        })
        plan = MotionPlanV2.model_validate(raw)
        self.assertAlmostEqual(plan.scenes[0].placement.x, 0.4)
        self.assertAlmostEqual(plan.scenes[0].placement.y, 0.09)
        self.assertAlmostEqual(plan.scenes[0].layers[1].geometry.x, 0.5)
        self.assertEqual(plan.scenes[0].layers[1].shape, "underline")
        slide = next(track for track in plan.scenes[0].tracks if track.property == "x")
        self.assertEqual(slide.keyframes[0].value, -0.08)

    def test_archive_registry_integrity_and_router_only_offer_executors(self):
        root = Path(__file__).resolve().parent
        registry = load_registry(root)
        self.assertEqual(registry["skill_count"], 36)
        self.assertEqual(registry["storyboard_count"], 30)
        self.assertTrue(all(item["integrity"] == "verified" for item in registry["skills"]))
        self.assertTrue(any(item["status"] == "reference_only" for item in registry["skills"]))
        selected = route_skills(root, task="kinetic", limit=4)
        self.assertTrue(1 <= len(selected) <= 4)
        self.assertTrue(all(item["executors"] and item["status"] in {"implemented", "partial"} for item in selected))


if __name__ == "__main__":
    unittest.main()
